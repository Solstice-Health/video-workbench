"""Real video probing/storage with deterministic substitutes for model/OCR work."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vwb.index import build
from vwb.index.paths import index_dir
from vwb.index.store import IndexStore
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.tools.media_tools import resolve_target
from vwb.util.procs import ffmpeg
from vwb.workspace import Workspace
from vwb.workspace.media import register_work_file
from vwb.workspace.request import SUBDIRS, Request


def workspace(root):
    ws = Workspace(root)
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="Edit every source"))
    return ws


@pytest.fixture
def ws(tmp_path):
    return workspace(tmp_path / "r-index")


@pytest.fixture
def sources(ws):
    refs = []
    for color, size, fps in [("red", "32x24", 10), ("blue", "48x32", 20)]:
        path = ws.work / f"{color}.mp4"
        ffmpeg("-f", "lavfi", "-i", f"color={color}:s={size}:r={fps}:d=0.3", "-an", "-c:v", "libx264", str(path), expect=[path])
        refs.append(register_work_file(ws, path.name))
    return refs


@pytest.fixture
def lightweight_stages(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(build, "Gateway", lambda *args, **kwargs: object())
    monkeypatch.setattr(build, "lever_serves", lambda _: False)
    monkeypatch.setattr("vwb.config.RUNS_DIR", tmp_path)

    def video_reading(gw, path, duration):
        calls.append(path)
        return {"summary": Path(path).name, "_model": "fixture"}

    def shots(builder):
        builder.store.replace_rows("shots", [{"id": 0, "f0": 0, "f1": builder.info["n_frames"],
                                             "t0": 0, "t1": builder.info["duration"],
                                             "title": Path(builder.src.original_path).stem}])

    def vision(builder):
        # Same sidecar name, different source bytes: must never collide.
        (builder.index / "ocr_samples.json").write_text(builder.src.sha256)

    monkeypatch.setattr(build.readings, "video_reading", video_reading)
    monkeypatch.setattr(build.IndexBuilder, "stage_shots", shots)
    monkeypatch.setattr(build.IndexBuilder, "stage_vision", vision)
    for name in ("stage_text", "stage_layout", "stage_reader3", "stage_marks"):
        monkeypatch.setattr(build.IndexBuilder, name, lambda _: None)
    return calls


def test_build_all_sources_keeps_facts_sidecars_and_stage_caches_independent(ws, sources, lightweight_stages):
    result = build.build_index(ws)
    assert [row["media"] for row in result["sources"]] == [src.ref for src in sources]
    assert index_dir(ws, sources[0].ref) == ws.index
    assert index_dir(ws, sources[1].ref) != ws.index
    for src, fps, title in zip(sources, [10, 20], ["red", "blue"], strict=True):
        folder = index_dir(ws, src.ref)
        st = IndexStore(folder / "index.sqlite")
        assert st.media()["media_id"] == src.media_id and st.media()["fps"] == fps
        assert st.one("SELECT title FROM shots")["title"] == title
        assert (folder / "ocr_samples.json").read_text() == src.sha256
        assert src.ref in (folder / "dossier.md").read_text()
        # Silent downloads must index without attempting to extract absent audio.
        assert st.all("SELECT * FROM sentences") == []
        assert st.one("SELECT t1 FROM silences")["t1"] == pytest.approx(0.3)
        st.connect().close()
    assert len(lightweight_stages) == 2
    build.build_index(ws)
    assert len(lightweight_stages) == 2
    first_dossier = (ws.index / "dossier.md").read_bytes()
    build.build_index(ws, media_id=sources[1].ref, force=True)
    assert len(lightweight_stages) == 3 and lightweight_stages[-1] == sources[1].path
    assert (ws.index / "dossier.md").read_bytes() == first_dossier


def test_index_and_frame_tools_select_second_source(ws, sources, lightweight_stages):
    build.build_index(ws)
    registry = build_registry()
    ctx = ToolContext(ws=ws, agent="lead", role="job_lead", gateway=None)
    overview = registry.call(ctx, "index_overview", {})
    assert all(src.ref in overview.text for src in sources)
    second = sources[1]
    for name, args in [("index_query", {"sql": "SELECT title FROM shots"}),
                       ("index_shots", {}), ("index_transcript", {}),
                       ("index_search", {"text": "blue"})]:
        result = registry.call(ctx, name, {"media_id": second.ref, **args})
        assert not result.is_error, result.text
        if name == "index_query":
            assert result.data["rows"] == [{"title": "blue"}]
        if name == "index_shots":
            assert result.data["shots"][0]["title"] == "blue"
        if name == "index_transcript":
            assert result.data["fps"] == 20
    path, fps, label = resolve_target(ctx, f"source:{second.ref}")
    assert (path, fps, label) == (Path(second.path), 20, second.ref)
    frame = registry.call(ctx, "media_frame", {"target": f"source:{second.ref}", "t": 0.1, "show": ["text"]})
    assert not frame.is_error and frame.images and frame.data["frame"] == 2
    with pytest.raises(ValueError, match="revision"):
        resolve_target(ctx, "source:src2@wrong")
    assert registry.call(ctx, "index_query", {"media_id": "src2@wrong", "sql": "SELECT * FROM media"}).is_error


def test_reuse_matches_the_actual_index_and_rebinds_request_local_identity(ws, sources, lightweight_stages, tmp_path):
    # The old code accepted a digest anywhere in the source list, even when
    # only src1's index existed. src2 must not inherit src1's facts or caches.
    build.build_index(ws, media_id=sources[0].ref)
    dest = workspace(tmp_path / "r-reuse")
    (dest.work / "blue.mp4").write_bytes(Path(sources[1].path).read_bytes())
    src = register_work_file(dest, "blue.mp4")
    assert src.media_id == "src1" and not build.reuse_index(dest, src)
    assert not (dest.index / "index.sqlite").exists()
    build.build_index(ws, media_id=sources[1].ref)
    origin = IndexStore(index_dir(ws, sources[1].ref) / "index.sqlite")
    origin.add_observation(media_id="src2", media_rev=sources[1].rev, kind="test", f0=0, f1=1,
                           value={}, producer="fixture")
    origin.add_observation(media_id="output01@v1", media_rev=sources[1].rev, kind="watch.answer", f0=0, f1=1,
                           value={}, producer="fixture")
    build.build_index(ws, media_id=sources[1].ref)
    assert origin.one("SELECT media_id FROM observations WHERE kind='watch.answer'")["media_id"] == "output01@v1"
    # Keep this connection open so the copied database must include WAL data.
    progress = []
    build.build_index(dest, progress=progress.append)
    assert any("reused the index" in message for message in progress)
    assert len(lightweight_stages) == 2
    st = IndexStore(dest.index / "index.sqlite")
    assert st.media()["path"] == src.path and st.media()["media_id"] == "src1"
    assert json.loads(st.media()["info"])["path"] == src.path
    assert st.one("SELECT media_id FROM observations WHERE kind='test'")["media_id"] == "src1"
    assert not st.all("SELECT * FROM observations WHERE kind='watch.answer'")
    assert st.one("SELECT title FROM shots")["title"] == "blue"
    assert src.ref in (dest.index / "dossier.md").read_text()
    assert not (dest.index / "sources").exists()
    origin.connect().close()
    st.connect().close()
