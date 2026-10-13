"""The request record (JobSpec) and the on-disk workspace for one request.

A workspace holds everything for one request. Source media are immutable revisions;
canvas versions are immutable; release/ is written only by the control plane.

    runs/<request-id>/
      request.json   media/   index/   assets/   canvas/   plates/   renders/
      qc/   ledger/   asks/   memory/   swarm/   release/   review/   work/
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from vwb.config import RUNS_DIR
from vwb.util.hashing import sha256_file, short
from vwb.util.jsonio import read_json, write_json

SUBDIRS = ("media", "index", "assets", "canvas", "plates", "renders", "qc", "ledger", "asks",
           "memory", "swarm", "release", "review", "work")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SourceRef(BaseModel):
    media_id: str                 # stable id inside the request, e.g. "src1"
    path: str                     # immutable copy inside media/
    original_path: str
    sha256: str

    @property
    def rev(self) -> str:
        return short(self.sha256)

    @property
    def ref(self) -> str:
        return f"{self.media_id}@{self.rev}"


class AssumedDefault(BaseModel):
    item: str
    value: Any
    why: str = ""


class JobSpec(BaseModel):
    """What the job lead understood the request to be. Recorded through the job_spec tool."""
    summary: str = ""
    outputs: int | None = None
    profile: str | None = None
    recipes: list[str] = Field(default_factory=list)       # e.g. ["clip", "resize"]; [] means open mode
    duration_s: list[float] | None = None                  # [min, max]
    constraints: list[str] = Field(default_factory=list)
    assumed_defaults: list[AssumedDefault] = Field(default_factory=list)


class ChatTurn(BaseModel):
    role: str                     # human | job_lead
    text: str
    context: dict[str, list[str]] = Field(default_factory=dict)
    ts: str = Field(default_factory=now_iso)


class Request(BaseModel):
    id: str
    created: str = Field(default_factory=now_iso)
    message: str
    sources: list[SourceRef] = Field(default_factory=list)
    spec: JobSpec | None = None
    status: str = "open"          # open | working | in_review | accepted
    chat: list[ChatTurn] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)  # output ids, in order

    def source(self, media_id: str | None = None) -> SourceRef:
        if not self.sources:
            raise KeyError("Request has no source media")
        if media_id is None:
            return self.sources[0]
        media_id = media_id.split("@")[0]
        for s in self.sources:
            if s.media_id == media_id:
                return s
        raise KeyError(f"No source media {media_id!r}")


def _slug(text: str, limit: int = 32) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:limit].strip("-") or "request"


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- paths ----
    def __getattr__(self, name: str) -> Path:
        if name in SUBDIRS:
            return self.root / name
        raise AttributeError(name)

    @property
    def request_path(self) -> Path:
        return self.root / "request.json"

    @property
    def id(self) -> str:
        return self.root.name

    # ---- lifecycle ----
    @classmethod
    def create(cls, message: str, sources: list[Path], request_id: str | None = None) -> "Workspace":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name_hint = sources[0].stem if sources else message
        rid = request_id or f"r-{stamp}-{_slug(name_hint)}"
        ws = cls(RUNS_DIR / rid)
        ws.root.mkdir(parents=True, exist_ok=False)
        for sub in SUBDIRS:
            (ws.root / sub).mkdir(exist_ok=True)
        refs = []
        for i, src in enumerate(sources, start=1):
            refs.append(ws.add_source(src, f"src{i}"))
        req = Request(id=rid, message=message, sources=refs, chat=[ChatTurn(role="human", text=message)])
        ws.save(req)
        return ws

    @classmethod
    def open(cls, request_id: str) -> "Workspace":
        root = Path(request_id)
        if not root.is_absolute():
            root = RUNS_DIR / request_id
        if not (root / "request.json").is_file():
            raise FileNotFoundError(f"No request workspace at {root}")
        return cls(root)

    @classmethod
    def latest(cls) -> "Workspace":
        candidates = sorted((p for p in RUNS_DIR.glob("r-*") if (p / "request.json").is_file()),
                            key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError("No request workspaces yet")
        return cls(candidates[-1])

    def add_source(self, path: Path, media_id: str) -> SourceRef:
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = sha256_file(path)
        dest = self.media / f"{media_id}_{short(digest)}{path.suffix.lower()}"
        if not dest.exists():
            try:
                os.link(path, dest)          # same volume: a hard link costs nothing
            except OSError:
                shutil.copy2(path, dest)
        return SourceRef(media_id=media_id, path=str(dest), original_path=str(path), sha256=digest)

    # ---- request record ----
    def load(self) -> Request:
        return Request.model_validate(read_json(self.request_path))

    def save(self, req: Request) -> None:
        write_json(self.request_path, req.model_dump(mode="json"))

    def update(self, **changes: Any) -> Request:
        req = self.load()
        for key, value in changes.items():
            setattr(req, key, value)
        self.save(req)
        return req

    def add_chat(self, role: str, text: str, *, context: dict[str, list[str]] | None = None) -> None:
        req = self.load()
        req.chat.append(ChatTurn(role=role, text=text, context=context or {}))
        self.save(req)

    def rel(self, path: Path | str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)
