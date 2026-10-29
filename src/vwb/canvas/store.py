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
from vwb.canvas.paths import checked_path, output_id, read_record, read_text, version_number
from vwb.canvas.renderers import validate_renderer
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace


class CanvasStore:
    def __init__(self, canvas_dir: Path):
        self.dir = Path(canvas_dir)
        checked_path(self.dir.parent, self.dir.name)
        self.dir.mkdir(parents=True, exist_ok=True)

    def head(self, output: str) -> int | None:
        output_id(output)
        try:
            return version_number(int(read_text(self.dir.parent, self.dir.name, output, "HEAD").strip()))
        except FileNotFoundError:
            return None

    def outputs(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir()
                      if not p.is_symlink() and p.is_dir() and (p / "HEAD").exists())

    def path(self, output: str, version: int) -> Path:
        return checked_path(self.dir.parent, self.dir.name, output_id(output), f"v{version_number(version)}.json")

    def get(self, output: str, version: int | None = None) -> Canvas:
        output_id(output)
        version = self.head(output) if version is None else version_number(version)
        if version is None:
            raise KeyError(f"No committed canvas for output {output!r}")
        return Canvas.model_validate(read_record(self.dir.parent, self.dir.name, output, f"v{version}.json"))

    def commit(self, ws: Workspace, output: str, author: str, message: str = "") -> tuple[Canvas, list[dict[str, Any]]]:
        """Check, expand and snapshot the working canvas. Raises with the errors if any."""
        canvas = load_working(ws, output)
        problems = [p.to_dict() for p in check(ws, canvas)]
        errors = [p for p in problems if p["severity"] == "error"]
        if errors:
            raise ValueError("canvas has errors:\n" + "\n".join(f"  {e['where']}: {e['why']}" for e in errors))
        canvas = expand(ws, canvas)
        canvas.renderer_pin = validate_renderer(canvas).pin
        from vwb.render.environment import bind
        bind(canvas)
        out_dir = checked_path(self.dir.parent, self.dir.name, output_id(output))
        out_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(out_dir / ".lock")):
            head = self.head(output)
            canvas.version = (head or 0) + 1
            canvas.parent = head
            canvas.provenance = {**canvas.provenance, "author": author, "message": message,
                                 "committed": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            # Capture the host's active model, rather than an agent-authored claim.
            active = read_json(ws.memory / "active_host.json", {}) or {}
            model = str(active.get("key", "")).partition(":")[2]
            canvas.provenance.pop("author_model", None)
            if model:
                canvas.provenance["author_model"] = model
            data = canvas.dump()
            data["provenance"]["sha256"] = sha256_json({k: v for k, v in data.items() if k != "provenance"})
            path = self.path(output, canvas.version)
            if path.exists():
                raise FileExistsError(f"{path} exists; canvas versions are immutable")
            write_json(path, data)
            checked_path(self.dir.parent, self.dir.name, output, "HEAD").write_text(str(canvas.version))
        return self.get(output, canvas.version), [p for p in problems if p["severity"] == "warn"]

    def diff(self, output: str, a: int, b: int) -> dict[str, Any]:
        ca, cb = self.get(output, a).dump(), self.get(output, b).dump()
        la = {l["id"]: l for l in ca["layers"]}
        lb = {l["id"]: l for l in cb["layers"]}
        return {"from": a, "to": b,
                "changed_keys": sorted(k for k in set(ca) | set(cb)
                                       if k not in ("version", "parent", "provenance") and ca.get(k) != cb.get(k)),
                "layers": {"added": sorted(set(lb) - set(la)), "removed": sorted(set(la) - set(lb)),
                           "changed": sorted(i for i in set(la) & set(lb) if la[i] != lb[i])}}
