"""Optional video-index enrichment and complete matte caching; no models/network."""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from vwb.gen import matting
from vwb.index.paths import index_dir
from vwb.index.store import IndexStore
from vwb.tools.base import ToolContext
from vwb.tools.canvas_tools import layout_facts
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry
from vwb.workspace.request import SUBDIRS, Request, SourceRef


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "r-optional-index")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True)
    src = SourceRef(media_id="src1", path=str(ws.media / "source.mkv"),
                    original_path="source.mkv", sha256="a" * 64)
    ws.save(Request(id=ws.id, message="Compose without indexing", sources=[src]))
    metadata_calls = []

    def metadata(path):
        metadata_calls.append(path)
        return SimpleNamespace(width=320, height=180, fps=Fraction(30000, 1001))

    monkeypatch.setattr("vwb.media.probe.video_stream_info", metadata)
    return ws, src, metadata_calls


def _facts(ws, **arguments):
    return layout_facts(ToolContext(ws, "job_lead", "job_lead", None), arguments).data


def _index(ws, src):
    store = IndexStore(index_dir(ws, src.ref) / "index.sqlite")
    # Deliberately different metadata: canvas facts must use the actual video.
    store.set_media(src.media_id, src.rev, src.path,
                    {"fps": 15, "width": 100, "height": 100, "n_frames": 60, "duration": 4})
    return store


def test_layout_facts_without_index_retains_profile_fonts_assets_and_source(workspace):
    ws, src, calls = workspace
    image = ws.work / "example.png"
    image.write_bytes(b"synthetic asset; layout facts only read registry metadata")
    ref = Registry(ws.assets).register(image, slot="example", kind="image", receipt="human.upload",
                                       derived_from=[], fit={"native_size": [16, 16]})
    facts = _facts(ws)
    assert facts["source"] == {"media": src.ref, "size": [320, 180], "fps": float(Fraction(30000, 1001))}
    assert facts["timeline"] == "source_spans"
    assert facts["canvas"] == [1080, 1920]
    assert facts["safe_zone"]["w"] > 0 and facts["safe_zone"]["h"] > 0
    assert facts["fonts"] and facts["legibility"] and facts["renderers"]
    assert facts["assets"] == [{"ref": ref, "kind": "image", "prov": "master", "size": [16, 16]}]
    assert facts["brand_style_from_readings"] == []
    assert calls == [src.path]
    assert not list(ws.index.rglob("*.sqlite"))


@pytest.mark.parametrize("state", ["empty", "stale", "corrupt"])
def test_layout_facts_tolerates_unusable_optional_index(workspace, state):
    ws, src, _ = workspace
    path = index_dir(ws, src.ref) / "index.sqlite"
    if state == "corrupt":
        path.write_bytes(b"not a SQLite database")
    else:
        store = IndexStore(path) if state == "empty" else _index(ws, src)
        if state == "stale":
            with store.connect() as con:
                con.execute("UPDATE media SET rev='stale'")
            store.insert_rows("readings", [{"scope": "video", "json": json.dumps({"style": "wrong source"})}])
        store.connect().close()
    facts = _facts(ws)
    assert facts["source"]["size"] == [320, 180]
    assert facts["fonts"] and facts["safe_zone"]
    assert facts["brand_style_from_readings"] == []


def test_layout_facts_reads_optional_style_without_using_index_geometry(workspace):
    ws, src, calls = workspace
    style = {"colours": ["#123456"]}
    store = _index(ws, src)
    store.insert_rows("readings", [{"scope": "video", "json": json.dumps({"style": style})},
                                   {"scope": "video", "json": "bad JSON"},
                                   {"scope": "video", "json": "[]"},
                                   {"scope": "video", "json": None},
                                   {"scope": "shot", "json": json.dumps({"style": "not video style"})}])
    store.connect().close()
    facts = _facts(ws)
    assert facts["brand_style_from_readings"] == [style]
    assert facts["source"]["size"] == [320, 180]
    assert calls == [src.path]


def test_layout_facts_source_selection_is_explicit_and_style_is_source_specific(workspace):
    ws, src, calls = workspace
    store = _index(ws, src)
    store.insert_rows("readings", [{"scope": "video", "json": json.dumps({"style": "first source only"})}])
    store.connect().close()
    second = src.model_copy(update={"media_id": "src2", "sha256": "b" * 64, "path": str(ws.media / "second.mkv")})
    request = ws.load()
    request.sources.append(second)
    ws.save(request)
    with pytest.raises(ValueError, match="specify media_id"):
        _facts(ws)
    assert not calls
    facts = _facts(ws, media_id=second.ref)
    assert facts["source"]["media"] == second.ref
    assert facts["brand_style_from_readings"] == []
    assert calls == [second.path]
    assert not (index_dir(ws, second.ref) / "index.sqlite").exists()


def test_layout_facts_source_free_needs_no_probe(workspace):
    ws, _, calls = workspace
    request = ws.load()
    request.sources.clear()
    ws.save(request)
    facts = _facts(ws)
    assert facts["source"] is None and facts["timeline"] == "authored_duration"
    assert facts["fonts"] and not calls


@pytest.fixture
def matte_stub(monkeypatch):
    state = SimpleNamespace(ranges=[], inference_calls=0, mode="complete", fail_inference=False)

    class Reader:
        def __init__(self, path):
            pass

        def frames(self, start, end):
            state.ranges.append((start, end))
            frames = list(range(start, end))
            if state.mode == "short":
                frames = frames[:-1]
            elif state.mode == "empty":
                frames = []
            elif state.mode == "gap":
                frames.pop(1)
            elif state.mode == "duplicate":
                frames.insert(1, start)
            elif state.mode == "extra":
                frames.append(end)
            for f in frames:
                shape = (32, 32, 3) if state.mode != "resize" or f == start else (34, 32, 3)
                yield f, np.zeros(shape, np.uint8)
            if state.mode == "late_failure":
                raise RuntimeError("decoder failed after yielding frames")

    class Session:
        def run(self, _, inputs):
            state.inference_calls += 1
            if state.fail_inference and state.inference_calls > 1:
                raise RuntimeError("inference failed")
            _, _, h, w = inputs["src"].shape
            alpha = np.zeros((1, 1, h, w), np.float32)
            alpha[:, :, 4:20, 2:14] = 1  # largest component, on the left
            alpha[:, :, 6:14, 24:30] = 1  # smaller component, on the right
            return inputs["src"], alpha, *(inputs[f"r{i}i"] for i in range(1, 5))

    monkeypatch.setattr(matting, "FrameReader", Reader)
    monkeypatch.setattr(matting, "session", lambda: Session())
    return state


def _matte(cache, **kwargs):
    return matting.matte_span(Path("unused.mkv"), "a" * 64, 3, 6, cache, **kwargs)


def test_matte_cache_separates_none_empty_and_populated_guidance(tmp_path, matte_stub):
    raw = _matte(tmp_path, warmup=1)
    largest = _matte(tmp_path, warmup=1, faces={})
    guided = _matte(tmp_path, warmup=1, faces={3: [(26, 6, 2, 4)]})
    assert raw.shape == largest.shape == guided.shape == (3, 16, 16)
    assert raw[0, 4, 13] == 255 and raw[0, 4, 4] == 255
    assert largest[0, 4, 13] == 0 and largest[0, 4, 4] > 0
    assert guided[0, 4, 13] > 0 and guided[0, 4, 4] == 0
    assert len(list(tmp_path.glob("*.npy"))) == 3
    assert not list(tmp_path.glob("*.part.npy"))
    calls = matte_stub.inference_calls
    repeated = _matte(tmp_path, warmup=1, faces={3: [[26.0, 6.0, 2.0, 4.0]]})
    np.testing.assert_array_equal(repeated, guided)
    assert not repeated.flags.writeable
    assert matte_stub.inference_calls == calls


def test_matte_cache_tracks_guidance_changes_warmup_and_version(tmp_path, matte_stub, monkeypatch):
    _matte(tmp_path, warmup=0, faces={3: [(26, 6, 2, 4)]})
    _matte(tmp_path, warmup=0, faces={3: [(4, 6, 2, 4)]})
    _matte(tmp_path, warmup=2, faces={3: [(4, 6, 2, 4)]})
    monkeypatch.setattr(matting, "VERSION", "test-next-version")
    _matte(tmp_path, warmup=2, faces={3: [(4, 6, 2, 4)]})
    assert matte_stub.ranges == [(3, 6), (3, 6), (1, 6), (1, 6)]
    assert len(list(tmp_path.glob("*.npy"))) == 4


def test_matte_cache_key_is_stable_for_guidance_order(tmp_path):
    first = {3: [(1, 2, 3, 4), (5, 6, 7, 8)], 5: [(9, 10, 11, 12)]}
    second = {5: [[9.0, 10.0, 11.0, 12.0]], 3: [[5, 6, 7, 8], [1, 2, 3, 4]]}
    assert matting.cache_path(tmp_path, "a" * 64, 3, 6, faces=first) == matting.cache_path(tmp_path, "a" * 64, 3, 6, faces=second)
    # The full source digest participates, including bytes beyond the visible prefix.
    assert matting.cache_path(tmp_path, "a" * 64, 3, 6) != matting.cache_path(tmp_path, "a" * 63 + "b", 3, 6)


@pytest.mark.parametrize("mode", ["empty", "short", "gap", "duplicate", "extra", "resize", "late_failure"])
def test_matte_failed_decode_leaves_no_partial_or_final_cache(tmp_path, matte_stub, mode):
    matte_stub.mode = mode
    with pytest.raises((ValueError, RuntimeError), match="matte decode|dimensions changed|decoder failed"):
        _matte(tmp_path, warmup=1)
    assert not list(tmp_path.iterdir())
    matte_stub.mode = "complete"
    assert _matte(tmp_path, warmup=1).shape == (3, 16, 16)
    assert len(list(tmp_path.iterdir())) == 1


def test_matte_failed_inference_leaves_no_partial_or_final_cache(tmp_path, matte_stub):
    matte_stub.fail_inference = True
    with pytest.raises(RuntimeError, match="inference failed"):
        _matte(tmp_path, warmup=1)
    assert not list(tmp_path.iterdir())
    matte_stub.fail_inference = False
    assert _matte(tmp_path, warmup=1).shape == (3, 16, 16)


@pytest.mark.parametrize("f0,f1,warmup", [(0, 0, 1), (3, 2, 1), (-1, 3, 1), (0, 3, -1),
                                       (0, 3, 1.5), (False, 3, 1)])
def test_invalid_matte_span_is_rejected_before_model_or_cache(tmp_path, monkeypatch, f0, f1, warmup):
    monkeypatch.setattr(matting, "session", lambda: pytest.fail("Invalid spans must not initialize the model"))
    with pytest.raises(ValueError):
        matting.matte_span(Path("unused"), "a" * 64, f0, f1, tmp_path / "cache", warmup=warmup)
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("faces", [{3: [(1, 2, -3, 4)]}, {3: [(float("nan"), 2, 3, 4)]}, {"3": [(1, 2, 3, 4)]}])
def test_invalid_face_guidance_is_rejected_before_model_or_cache(tmp_path, monkeypatch, faces):
    monkeypatch.setattr(matting, "session", lambda: pytest.fail("Invalid guidance must not initialize the model"))
    with pytest.raises(ValueError):
        _matte(tmp_path / "cache", faces=faces)
    assert not (tmp_path / "cache").exists()


def test_outside_face_guidance_cannot_select_pixels_by_negative_slice():
    alpha = np.zeros((40, 40), np.uint8)
    alpha[2:5, 2:5] = 255
    alpha[20:30, 20:30] = 255
    expected = matting._person_only(alpha, [], 1)
    actual = matting._person_only(alpha, [(-15, 2, 1, 1)], 1)
    np.testing.assert_array_equal(actual, expected)
