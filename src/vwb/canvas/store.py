"""Immutable canvas versions with a HEAD per output. Only the job lead commits (spec D08).

The agent edits a working file (work/outputs/<id>/canvas.json) like source code. A commit
checks it, expands what code derives (copy entries, caption pages), and snapshots an
immutable version canvas/<id>/v<N>.json, like a git commit. Renders cite versions only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from vwb.canvas.compile import check, expand, load_working
from vwb.canvas.model import Canvas
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace


class CanvasStore:
    def __init__(self, canvas_dir: Path):
        self.dir = Path(canvas_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def head(self, output: str) -> int | None:
        p = self.dir / output / "HEAD"
        return int(p.read_text().strip()) if p.exists() else None

    def outputs(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if p.is_dir() and (p / "HEAD").exists())

    def path(self, output: str, version: int) -> Path:
        return self.dir / output / f"v{version}.json"

    def get(self, output: str, version: int | None = None) -> Canvas:
        version = version or self.head(output)
        if version is None:
            raise KeyError(f"No committed canvas for output {output!r}")
        return Canvas.model_validate(read_json(self.path(output, version)))

    def commit(self, ws: Workspace, output: str, author: str, message: str = "") -> tuple[Canvas, list[dict[str, Any]]]:
        """Check, expand and snapshot the working canvas. Raises with the errors if any."""
        canvas = load_working(ws, output)
        problems = [p.to_dict() for p in check(ws, canvas)]
        errors = [p for p in problems if p["severity"] == "error"]
        if errors:
            raise ValueError("canvas has errors:\n" + "\n".join(f"  {e['where']}: {e['why']}" for e in errors))
        canvas = expand(ws, canvas)
        out_dir = self.dir / output
        out_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(out_dir / ".lock")):
            head = self.head(output)
            canvas.version = (head or 0) + 1
            canvas.parent = head
            canvas.provenance = {**canvas.provenance, "author": author, "message": message,
                                 "committed": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            data = canvas.dump()
            data["provenance"]["sha256"] = sha256_json({k: v for k, v in data.items() if k != "provenance"})
            path = self.path(output, canvas.version)
            if path.exists():
                raise FileExistsError(f"{path} exists; canvas versions are immutable")
            write_json(path, data)
            (out_dir / "HEAD").write_text(str(canvas.version))
        return Canvas.model_validate(read_json(path)), [p for p in problems if p["severity"] == "warn"]

    def diff(self, output: str, a: int, b: int) -> dict[str, Any]:
        ca, cb = self.get(output, a).dump(), self.get(output, b).dump()
        la = {l["id"]: l for l in ca["layers"]}
        lb = {l["id"]: l for l in cb["layers"]}
        return {"from": a, "to": b,
                "changed_keys": sorted(k for k in set(ca) | set(cb)
                                       if k not in ("version", "parent", "provenance") and ca.get(k) != cb.get(k)),
                "layers": {"added": sorted(set(lb) - set(la)), "removed": sorted(set(la) - set(lb)),
                           "changed": sorted(i for i in set(la) & set(lb) if la[i] != lb[i])}}
