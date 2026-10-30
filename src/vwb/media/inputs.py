"""Video inputs shared by inspection and generation, without an edit index.

Work files are snapshotted automatically. Attachment references always select
their immutable original, never the editable attachment copy under work/.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from vwb.index.paths import source
from vwb.media.images import _original
from vwb.media.probe import video_stream_info
from vwb.web.workspace_files import open_file
from vwb.workspace import Workspace, attachments
from vwb.workspace.media import _snapshot, register_work_file
from vwb.workspace.registry import Registry


@dataclass(frozen=True)
class ResolvedVideo:
    path: Path
    ref: str
    lineage: dict[str, Any]


def _signature(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@lru_cache(maxsize=128)
def _verify_digest(root: str, relative: str, expected: str, signature: tuple):
    # Hash once per file identity/revision, not for every requested frame.
    # Pinned descriptors reject symlinks; changes invalidate the cached check.
    with open_file(Workspace(Path(root)), relative) as stream:
        if _signature(os.fstat(stream.fileno())) != signature:
            raise ValueError("Video changed during validation; retry with the current file.")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected or _signature(os.fstat(stream.fileno())) != signature:
            raise ValueError("Video integrity check failed: the original bytes changed after the receipt was saved.")


def _checked(ws: Workspace, path: str | Path, expected: str) -> Path:
    path = Path(path)
    relative = path.relative_to(ws.root).as_posix()
    with open_file(ws, relative) as stream:
        signature = _signature(os.fstat(stream.fileno()))
    _verify_digest(str(ws.root), relative, expected, signature)
    video_stream_info(path)  # cached geometry/timing; no content hash or index
    return path


def resolve_video(ws: Workspace, ref: str) -> ResolvedVideo:
    """Resolve source[:ref], slot:name[@rev], attachment:id or a work path.

    Returns a stable path, pinned canonical reference and byte-level lineage.
    No indexing/model call occurs. Existing source/slot references are resolved
    without copying their video again; hash verification is cached by file identity.
    """
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 4096:
        raise ValueError("Use a source/slot reference, attachment:<id>, or work-relative video path.")
    ref = ref.strip()
    if ref == "source" or ref.startswith("source:"):
        src = source(ws, ref.partition(":")[2] or None)
        canonical = f"source:{src.ref}"
        return ResolvedVideo(_checked(ws, src.path, src.sha256), canonical,
                             {"kind": "source", "media": src.ref, "ref": canonical, "sha256": src.sha256})
    if ref.startswith("slot:"):
        row = Registry(ws.assets).get(ref)
        if row["kind"] != "video":
            raise ValueError("This asset is not a video. Use image_inspect for a still image.")
        canonical = f"slot:{row['slot']}@{row['rev']}"
        return ResolvedVideo(_checked(ws, row["path"], row["sha256"]), canonical,
                             {"kind": "asset", "ref": canonical, "sha256": row["sha256"], "prov": row["prov"]})
    if ref.startswith("attachment:"):
        row = attachments.canonical(ws, ref[11:])
        if row["kind"] != "video":
            raise ValueError("This attachment is not a video. Use image_inspect or the document tools for its type.")
        original = attachments.original_path(ws, row)
        src = next((item for item in ws.load().sources if item.sha256 == row["sha256"]), None)
        if src is None:
            # Pin every parent and the original inode; source_paths' optional
            # filename alias is unnecessary and must not redirect this input.
            with _original(ws, row["id"]) as incoming:
                src = _snapshot(ws, incoming, row["name"], str(original),
                                validate=video_stream_info, expected_sha=row["sha256"])
        canonical = f"source:{src.ref}"
        return ResolvedVideo(_checked(ws, src.path, src.sha256), canonical,
                             {"kind": "attachment", "id": row["id"], "ref": f"attachment:{row['id']}",
                              "media": src.ref, "sha256": src.sha256})
    relative = ref[5:] if ref.startswith(("work:", "work/")) else ref
    src = register_work_file(ws, relative, validate=video_stream_info)
    canonical = f"source:{src.ref}"
    return ResolvedVideo(_checked(ws, src.path, src.sha256), canonical,
                         {"kind": "work_file", "path": relative, "ref": canonical,
                          "media": src.ref, "sha256": src.sha256})
