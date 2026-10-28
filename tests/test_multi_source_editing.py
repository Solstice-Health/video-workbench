"""Multi-source edits with overlapping local IDs; tiny real media, no paid/model calls."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from vwb.canvas import copydeck
from vwb.canvas.compile import check, expand, output_words
from vwb.canvas.model import Canvas, font_library
from vwb.canvas.store import CanvasStore
from vwb.config import load_profile
from vwb.gen import matting
from vwb.gen.refs import resolve
from vwb.index.paths import SourceIndexes, index_dir
from vwb.index.store import IndexStore
from vwb.media.decode import read_frames
from vwb.media.probe import probe
from vwb.qc import inspect
from vwb.render import audio_mix, pipeline
from vwb.render.compositor import Compositor
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.tools.gen_tools import _mask_from
from vwb.util.jsonio import read_json, write_json
from vwb.util.procs import ffmpeg, run, which
from vwb.verify import documents, sidecars
from vwb.verify.verifier import standing_findings, verify
from vwb.workspace import Workspace
from vwb.workspace.media import register_work_file
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "r-edit")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True)
    ws.save(Request(id=ws.id, message="Edit all videos"))
    monkeypatch.setattr("vwb.workspace.ledger.control_key", lambda: b"test-multi-source-editing")
    refs = []
    for i, (color, size, fps) in enumerate([("red", "96x64", 30), ("blue", "192x128", 60)]):
        path = ws.work / f"{color}.mp4"
        audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.8", "-c:a", "aac"] if i else ["-an"]
        ffmpeg("-f", "lavfi", "-i", f"color={color}:s={size}:r={fps}:d=0.8", *audio,
               "-c:v", "libx264", "-t", "0.8", str(path), expect=[path])
        src = register_work_file(ws, path.name)
        refs.append(src)
        st = IndexStore(index_dir(ws, src.ref) / "index.sqlite")
        info = probe(src.path, with_keyframes=False).to_dict()
        st.set_media(src.media_id, src.rev, src.path, info)
        st.replace_rows("words", [{"i": 0, "f0": int(fps * .2), "f1": int(fps * .4), "t0": .2, "t1": .4,
                                   "text": color, "norm": color, "agree": 1, "sentence": 0}])
        st.replace_rows("sentences", [{"id": 0, "f0": int(fps * .2), "f1": int(fps * .4), "t0": .2, "t1": .4,
                                       "text": color, "w0": 0, "w1": 1, "agree": 1, "shot": 0}])
        st.replace_rows("shots", [{"id": 0, "f0": 0, "f1": info["n_frames"], "t0": 0, "t1": .8,
                                   "title": f"{color} shot", "kind": "content"}])
        st.replace_rows("text_tracks", [{"id": 0, "f0": 0, "f1": info["n_frames"], "t0": 0, "t1": .8,
                                         "x": 150 if i else 5, "y": 90 if i else 5, "w": 30, "h": 20,
                                         "text": f"{color} claim", "text2": f"{color} claim", "agree": 1,
                                         "cap_h": 14, "role": "claim" if i else "body", "regulated": i, "shot": 0}])
        st.replace_rows("faces", [{"f": 12, "x": 150 if i else 5, "y": 90 if i else 5, "w": 20, "h": 20}])
        st.replace_rows("marks", [{"id": 0, "name": color, "kind": "logo", "f0": 0, "f1": 12,
                                   "x": 5, "y": 5, "w": 20, "h": 20, "best_frame": 6}])
        if i:
            st.replace_rows("hazards", [{"id": 0, "kind": "claim", "f0": 12, "f1": 24, "t0": .2, "t1": .4,
                                         "text": "blue claim", "status": "fact"},
                                        {"id": 1, "kind": "job_code", "f0": 0, "f1": 48, "t0": 0, "t1": .8,
                                         "text": "BLUE-CODE", "status": "fact"}])
        st.connect().close()
    return ws, refs


def style():
    name, info = next(iter(font_library().items()))
    return {"font": name, "weight": info["weights"][0], "size": 12, "color": "#ffffff"}


def canvas(ws, refs, *, spans=None, layers=None):
    return Canvas.model_validate({"request": ws.id, "output": "edit", "profile": "youtube_shorts",
                                  "format": {"width": 96, "height": 64, "fps": 30},
                                  "time_map": spans or [{"media": refs[0].ref, "src_in": 0, "src_out": 18},
                                                        {"media": refs[1].ref, "src_in": 0, "src_out": 30}],
                                  "layers": layers or []})


def call(ws, name, args):
    return build_registry().call(ToolContext(ws, "lead", "job_lead", None), name, args)


def test_captions_and_sidecars_keep_source_identity_across_cuts_and_repeats(setup):
    ws, refs = setup
    c = canvas(ws, refs, layers=[{"id": "captions", "type": "captions", "box": [0, 0, 96, 64], "style": style()}])
    c.time_map.append(c.time_map[0].model_copy())
    expand(ws, c)
    assert [e.text for e in c.copy_deck] == ["red", "blue", "red"]
    assert [e.source["media"] for e in c.copy_deck] == [refs[0].ref, refs[1].ref, refs[0].ref]
    assert [e.regulated for e in c.copy_deck] == [False, True, False]
    pages = c.layers[0].pages
    assert pages[0]["to"] <= 18 and pages[1]["to"] <= 48
    words = output_words(SourceIndexes(ws), c)
    assert [w["o0"] for w in words] == [6, 30, 54]
    assert [w["span_index"] for w in words] == [0, 1, 2]
    srt = sidecars.captions_srt(SourceIndexes(ws), c)
    assert srt.index("red") < srt.index("blue") < srt.rindex("red")
    cue = documents.cue_sheet(ws, c, SourceIndexes(ws), {"sha256": "test"})
    assert [r["shots"][0]["title"] for r in cue["segments"]] == ["red shot", "blue shot", "red shot"]
    assert cue["segments"][1]["src_out"] == "00:00.500"
    desc = sidecars.description(ws, SourceIndexes(ws), c)
    assert refs[0].ref in desc and refs[1].ref in desc


def test_crop_check_and_compositor_follow_layer_source(setup):
    ws, refs = setup
    c = canvas(ws, refs, layers=[{"id": "red", "type": "video", "source": refs[0].ref, "box": [0, 0, 48, 64], "fit": "cover"},
                                 {"id": "blue", "type": "video", "source": refs[1].ref, "box": [48, 0, 48, 64],
                                  "crop": [100, 40, 48, 64], "fit": "cover"}])
    assert not [p for p in check(ws, c) if p.severity == "error"]
    frames = Compositor(ws, c).sample([6, 30])
    assert frames[6][20, 20, 0] > 200 and frames[6][:, 48:].max() == 0
    assert frames[30][20, 70, 2] > 200 and frames[30][:, :48].max() == 0
    c.layers[0].crop = [100, 40, 48, 64]
    assert any("96x64 source frame" in p.why for p in check(ws, c))


def test_qc_never_confuses_source_local_track_ids_or_words(setup):
    ws, refs = setup
    red_copy = copydeck.mint(ws, copy_id="red-copy", role="title", source="ocr", tracks=[0], media_id=refs[0].ref)
    c = canvas(ws, refs, layers=[{"id": "red-copy", "type": "text", "copy_id": red_copy.id,
                                 "box": [0, 0, 96, 64], "style": style()}])
    c.copy_deck = [red_copy]
    findings = inspect.declared(c, SourceIndexes(ws), load_profile(c.profile), ws=ws)
    dropped = next(f for f in findings if f.kind == "TEXT-DROPPED")
    assert dropped.evidence["tracks"] == [{"id": 0, "media": refs[1].ref, "role": "claim", "text": "blue claim"}]
    # Frame 18 is .3s in blue's 60fps source, inside its word, despite being
    # .6s on the 30fps output clock. Red's frame 18 is outside its word.
    c.time_map[1].src_in = 18
    result = inspect.boundaries(c, SourceIndexes(ws))[0]
    assert result.status == "fail" and all(cut["media"] == refs[1].ref for cut in result.evidence["cuts"])
    rules = standing_findings(ws, c, SourceIndexes(ws))
    assert next(f for f in rules if f.check == "rule.job_code").evidence["media"] == [refs[1].ref]
    c.time_map = [c.time_map[0]]
    assert not any(f.check == "rule.job_code" for f in standing_findings(ws, c, SourceIndexes(ws)))


def test_audio_readback_uses_ordered_transcripts_from_every_span(setup, monkeypatch):
    ws, refs = setup
    c = canvas(ws, refs)
    monkeypatch.setattr(inspect, "ffmpeg", lambda *a, **kw: None)
    monkeypatch.setattr("vwb.index.asr.transcribe_reader1", lambda *a: [{"text": "red", "t0": .2}, {"text": "blue", "t0": 1.0}])
    result = inspect.audio_readback(c, SourceIndexes(ws), Path("unused.mp4"))
    assert all(f.status == "pass" for f in result)
    with pytest.raises(ValueError, match="cannot inspect"):
        output_words(SourceIndexes(ws).for_media(refs[0].ref), c)


def test_matting_receives_faces_from_the_selected_source(setup, monkeypatch):
    ws, refs = setup
    seen = []

    def matte(path, sha, f0, f1, cache, **kw):
        seen.append((path, sha, kw["faces"]))
        return np.full((f1 - f0, 64, 96), 255, dtype=np.uint8)

    monkeypatch.setattr(matting, "matte_span", matte)
    c = canvas(ws, refs, layers=[{"id": "person", "type": "video", "source": refs[1].ref,
                                 "box": [0, 0, 96, 64], "matte": "person"}])
    Compositor(ws, c).sample([30])
    assert len(seen) == 1 and seen[0][0] == Path(refs[1].path)
    assert seen[0][2][12][0][0] == 150
    result = call(ws, "video_matte", {"media_id": refs[1].ref, "t0": 0, "t1": .3})
    assert not result.is_error and result.data["span_frames"] == [0, 18]
    assert seen[-1][2][12][0][0] == 150


def test_source_aware_authoring_and_legacy_helpers_require_explicit_selection(setup):
    ws, refs = setup
    for name, args in [("canvas_new", {"output": "ambiguous", "spans": [[0, 18]]}),
                       ("copy_add", {"id": "ambiguous", "source": "ocr", "tracks": [0]}),
                       ("clip_refine", {"t0": 0, "t1": .6}), ("asset_marks", {}),
                       ("focus_find", {"spans": [[0, 18]]}), ("video_matte", {"t0": 0, "t1": .3})]:
        result = call(ws, name, args)
        assert result.is_error and "specify media_id" in result.text, result.text
    copied = call(ws, "copy_add", {"id": "blue", "source": "ocr", "tracks": [0], "media_id": refs[1].ref})
    assert copied.data["text"] == "blue claim" and copied.data["source"]["media"] == refs[1].ref
    marks = call(ws, "asset_marks", {"media_id": refs[1].ref})
    assert marks.data["marks"][0]["name"] == "blue"
    recovered = call(ws, "asset_recover", {"media_id": refs[1].ref, "mark_id": 0})
    assert not recovered.is_error and recovered.data["media"] == refs[1].ref
    from vwb.workspace.registry import Registry
    lineage = json.loads(Registry(ws.assets).get(recovered.data["badge"])["derived_from"])
    assert lineage[0]["media"] == refs[1].ref
    focused = call(ws, "focus_find", {"media_id": refs[1].ref, "spans": [[0, 30]]})
    assert not focused.is_error and any(b["text"] == "blue" for b in focused.data["beats"])
    solved = call(ws, "reframe_solve", {"media_id": refs[1].ref, "spans": [[0, 30]], "box": [0, 0, 96, 64], "k": 1})
    assert not solved.is_error and solved.data["media"] == refs[1].ref
    assert read_json(ws.work / solved.data["saved"])["media"] == refs[1].ref
    refined = call(ws, "clip_refine", {"media_id": refs[1].ref, "t0": .2, "t1": .4})
    assert not refined.is_error and refined.data["f_out"] > 24
    report = call(ws, "clip_validate", {"outputs": [{"id": "red", "media_id": refs[0].ref, "spans": [[0, 24]]},
                                                     {"id": "blue", "media_id": refs[1].ref, "spans": [[0, 24]]}]})
    assert [o["duration"] for o in report.data["outputs"]] == [.8, .4] and not report.data["pairs"]
    mapped = call(ws, "canvas_new", {"output": "mapped", "time_map": [{"media": refs[1].ref, "src_in": 0, "src_out": 18},
                                                                         {"media": refs[0].ref, "src_in": 0, "src_out": 12}]})
    assert not mapped.is_error and mapped.data["sources"] == [refs[1].ref, refs[0].ref]
    resolved = resolve(ws, f"source:{refs[1].ref}#t=0.2")
    assert resolved.lineage["media"] == refs[1].ref and resolved.lineage["frame"] == 12
    assert resolved.rgba[20, 20, 2] > 200
    with pytest.raises(ValueError, match="specify media_id"):
        resolve(ws, "f:12")
    mask = _mask_from(ToolContext(ws, "lead", "job_lead", None), {"mask": {"text_tracks": [0], "pad": 0}},
                      f"source:{refs[1].ref}#f=12", (128, 192))
    assert mask[95, 155] and not mask[10, 10]


def test_tiny_real_multi_source_render_and_review(setup, monkeypatch):
    ws, refs = setup
    c = canvas(ws, refs, layers=[{"id": src.media_id, "type": "video", "source": src.ref,
                                 "box": [0, 0, 96, 64]} for src in refs])
    write_json(ws.work / "outputs/edit/canvas.json", c.dump())
    committed, _ = CanvasStore(ws.canvas).commit(ws, "edit", "test")
    receipt = pipeline.render(ws, "edit")
    info = probe(receipt["file"], with_keyframes=False)
    assert info.n_frames == c.length == 48 and info.has_audio
    assert info.duration == pytest.approx(1.6, abs=.02)
    frames = read_frames(receipt["file"], [6, 30])
    assert frames[6][20, 20, 0] > 200 and frames[30][20, 20, 2] > 200
    # No OCR/ASR model is needed for this no-caption fixture. Review still uses
    # real declared checks, boundary checks, re-render attestation and sidecars.
    monkeypatch.setattr("vwb.verify.verifier.lever_serves", lambda _: False)
    report = verify(ws, committed.output)
    assert report["attested"]
    qdir = ws.qc / "edit/v1"
    cue = read_json(qdir / "cue_sheet.json")
    assert [s["media"] for s in cue["segments"]] == [src.ref for src in refs]
    assert "blue" in (qdir / "captions.srt").read_text()


def test_audio_mix_keeps_silent_prefix_and_selected_audio_duration(setup):
    ws, refs = setup
    c = canvas(ws, refs)
    out = ws.renders / "mixed.m4a"
    audio_mix.mix(ws, c, out)
    pcm = run([which("ffmpeg"), "-v", "error", "-i", str(out), "-f", "f32le", "-ac", "1", "-ar", "48000", "-"])
    values = np.frombuffer(pcm.stdout, dtype=np.float32)
    assert len(values) / 48000 == pytest.approx(1.6, abs=.04)
    assert np.max(np.abs(values[:int(.5 * 48000)])) < .001
    assert np.sqrt(np.mean(values[int(.8 * 48000):int(1.3 * 48000)] ** 2)) > .01
    c.time_map = [c.time_map[0]]
    assert audio_mix.mix(ws, c, ws.renders / "silent.m4a")["mode"] == "silent"
