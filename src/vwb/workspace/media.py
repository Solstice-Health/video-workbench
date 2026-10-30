"""Promote explicit video inputs to request-local immutable, digest-deduplicated sources."""
from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from filelock import FileLock

from vwb.workspace import attachments, references
from vwb.workspace.request import SourceRef, Workspace


def _snapshot(ws: Workspace, incoming: BinaryIO, name: str, original_path: str, *,
              validate: Callable[[Path], object] | None = None, expected_sha: str | None = None) -> SourceRef:
    """Copy an already pinned file; never hard-link mutable work/ into media/."""
    before = os.fstat(incoming.fileno())
    if before.st_size > attachments.MAX_BYTES:
        raise ValueError("Register a video no larger than 4 GB.")
    ws.media.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=ws.media, suffix=Path(name).suffix.lower(), delete=False) as out:
        staged = Path(out.name)
        try:
            count = 0
            while block := incoming.read(min(1024 * 1024, attachments.MAX_BYTES - count + 1)):
                count += len(block)
                if count > attachments.MAX_BYTES:
                    raise ValueError("The video grew beyond the 4 GB registration limit.")
                digest.update(block)
                out.write(block)
            after = os.fstat(incoming.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("The video changed during registration; finish writing it and retry media_register.")
            out.flush()
            if attachments.describe(staged, name)["kind"] != "video":
                raise ValueError("The selected file is not a supported video; media_register accepts videos only. For an image, pass its work-relative path or attachment:<id> directly to image_inspect, sunburst_inpaint/extract/outpaint, or image reference inputs. No video conversion, registration or index is needed.")
            sha = digest.hexdigest()
            if expected_sha is not None and sha != expected_sha:
                raise ValueError("The attachment changed during video snapshotting.")
            if validate is not None:
                validate(staged)  # Reject unreadable inputs before adding a source to the request.
            # Workers may register different downloads concurrently. Reload inside
            # the lock so IDs and digest deduplication use the latest source list.
            with FileLock(str(ws.root / "media.lock")):
                req = ws.load()
                existing = next((src for src in req.sources if src.sha256 == sha), None)
                if existing is not None:
                    return existing
                ids = {src.media_id for src in req.sources}
                i = 1
                while f"src{i}" in ids:
                    i += 1
                dest = ws.media / f"src{i}_{sha[:12]}{staged.suffix}"
                staged.chmod(0o444)
                os.replace(staged, dest)
                source = SourceRef(media_id=f"src{i}", path=str(dest), original_path=original_path, sha256=sha)
                req.sources.append(source)
                ws.save(req)
                return source
        finally:
            staged.unlink(missing_ok=True)


def register_work_file(ws: Workspace, path: str, *, validate: Callable[[Path], object] | None = None) -> SourceRef:
    """Agent/user-selected paths are relative to ws.work, never arbitrary host paths."""
    with references.open_file(ws, path) as incoming:
        return _snapshot(ws, incoming, Path(path).name, str(ws.work / path), validate=validate)


def register_inputs(ws: Workspace, refs: list[dict], context: dict[str, list[str]]) -> list[SourceRef]:
    """Register every video attachment and explicitly selected video file, not folders."""
    sources = []
    for ref, path in attachments.source_paths(ws, refs):
        with path.open("rb") as incoming:
            sources.append(_snapshot(ws, incoming, ref["name"], str(path)))
    for path in context.get("files", []):
        if Path(path).suffix.lower() in attachments.VIDEO_EXTENSIONS:
            sources.append(register_work_file(ws, path))
    return list({src.sha256: src for src in sources}.values())
