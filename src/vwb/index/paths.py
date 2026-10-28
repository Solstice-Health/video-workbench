"""One independent index and sidecar directory per registered source."""
from __future__ import annotations

from pathlib import Path

from vwb.index.store import IndexStore
from vwb.workspace import SourceRef, Workspace


def source(ws: Workspace, media_id: str | None = None, *, require_explicit: bool = False) -> SourceRef:
    req = ws.load()
    if require_explicit and media_id is None and len(req.sources) > 1:
        raise ValueError("Multiple sources are registered; specify media_id: " + ", ".join(s.ref for s in req.sources))
    src = req.source(media_id)
    if media_id and "@" in media_id and media_id != src.ref:
        raise ValueError(f"Unknown source revision {media_id!r}; current reference is {src.ref}.")
    return src


def index_dir(ws: Workspace, media_id: str | None = None) -> Path:
    src = source(ws, media_id)
    # Keep the first source at the legacy path for existing single-source clients.
    # Every other source owns its SQLite rows, stage cache, audio and OCR files.
    if src.media_id == ws.load().sources[0].media_id:
        return ws.index
    return ws.index / "sources" / src.sha256


class SourceIndexes:
    """Lazily route canvas spans/layers to their own source index."""

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.stores: dict[str, IndexStore] = {}

    def for_media(self, media: str) -> IndexStore:
        src = source(self.ws, media)
        if src.ref not in self.stores:
            path = index_dir(self.ws, src.ref) / "index.sqlite"
            if not path.is_file():
                raise ValueError(f"No index for {src.ref}; run index_build(media_id='{src.ref}').")
            self.stores[src.ref] = IndexStore(path)
        return store_for(self.stores[src.ref], src.ref)


def store_for(indexes: SourceIndexes | IndexStore, media: str) -> IndexStore:
    """Keep single-index callers compatible, but never read the wrong source."""
    if isinstance(indexes, SourceIndexes):
        return indexes.for_media(media)
    row = indexes.media()
    ref = f"{row['media_id']}@{row['rev']}"
    if media != ref:
        raise ValueError(f"Index for {ref} cannot inspect {media}; use SourceIndexes(workspace).")
    return indexes
