"""Original videos from briefs/design references: intake through an actual render."""
from __future__ import annotations

import base64
import io
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from vwb import orchestrator
from vwb.agents.job_lead import _opening, wake_context
from vwb.canvas.compile import check, load_working
from vwb.canvas.model import Canvas
from vwb.canvas.store import CanvasStore
from vwb.config import load_profile
from vwb.integrations.artifacts import import_result
from vwb.media.probe import probe
from vwb.qc import inspect
from vwb.render import pipeline
from vwb.render.compositor import Compositor
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.util.jsonio import read_json, write_json
from vwb.verify.verifier import verify
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry as AssetRegistry
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path):
    value = Workspace(tmp_path / "r-design")
    for sub in SUBDIRS:
        (value.root / sub).mkdir(parents=True, exist_ok=True)
    value.save(Request(id=value.id, message="Create a video from the Figma reference"))
    return value


def call(ws, name, arguments):
    ctx = ToolContext(ws, "job_lead", "job_lead", None)
    return build_registry().call(ctx, name, arguments)


def image_reference(ws, size=(64, 96)):
    pixels = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    pixels[:] = [63, 123, 171]
    data = io.BytesIO()
    Image.fromarray(pixels).save(data, "PNG")
    result = import_result(ws, "figma_direct_get_images", {"file_key": "design123", "node_ids": ["1:2"]},
                           {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(data.getvalue()).decode()}],
                            "structuredContent": {"file_key": "design123", "images": [{"node_id": "1:2", "source_url": "https://www.figma.com/design/design123?node-id=1-2"}]}},
                           "https://api.figma.com", provider="figma_direct")
    return result["artifacts"][0]["asset_ref"], pixels


def test_source_free_tools_create_copy_check_commit_preview_and_provenance(ws):
    assert call(ws, "index_build", {}).data["status"] == "not_applicable"
    assert "No source video" in call(ws, "index_overview", {}).text
    facts = call(ws, "layout_facts", {})
    assert not facts.is_error and facts.data["source"] is None
    assert facts.data["timeline"] == "authored_duration"
    made = call(ws, "canvas_new", {"output": "short-01", "duration_s": 2})
    assert not made.is_error and made.data["frames"] == 60 and made.data["media"] is None
    ref, pixels = image_reference(ws)
    row = AssetRegistry(ws.assets).get(ref)
    assert row["receipt"] == "integration.import" and row["prov"] == "original_pixel"
    assert json.loads(row["derived_from"])[0]["arguments"]["file_key"] == "design123"
    copied = call(ws, "copy_add", {"id": "design-title", "source": "authored", "text": "An authored title",
                                   "note": "Figma design123 node 1:2; imported response.json"})
    assert not copied.is_error and not copied.data["agree"]
    path = ws.work / made.data["path"]
    doc = read_json(path)
    doc["format"].update(width=64, height=96)
    doc["layers"] = [{"id": "design", "type": "image", "asset": ref, "box": [0, 0, 64, 96]}]
    write_json(path, doc)
    checked = call(ws, "canvas_check", {"output": "short-01"})
    assert not checked.is_error and not checked.data["errors"]
    committed = call(ws, "canvas_commit", {"output": "short-01", "message": "Design reference draft"})
    assert not committed.is_error
    c = CanvasStore(ws.canvas).get("short-01")
    comp = Compositor(ws, c)
    frames = list(comp.frames())
    assert len(frames) == 60 and frames[-1][0] == 59
    samples = comp.sample([0, 15, 59])
    assert set(samples) == {0, 15, 59}
    assert all(np.array_equal(frame, pixels) for frame in samples.values())
    assert inspect.audio_readback(c, None, Path("unused.mp4"))[0].status == "info"


@pytest.mark.parametrize("arguments", [{}, {"duration_s": 0}, {"duration_s": -1}, {"duration_s": float("nan")},
                                       {"duration_s": 0.0001}, {"duration_s": 100000},
                                       {"duration_s": 1, "spans": [[0, 30]]}, {"spans": [[0, 30]]}])
def test_timeline_tool_rejects_invalid_or_ambiguous_requests(ws, arguments):
    result = call(ws, "canvas_new", {"output": "short-01", **arguments})
    assert result.is_error
    assert not (ws.work / "outputs/short-01/canvas.json").exists()


def test_canvas_rejects_mixed_clocks_and_source_layers_on_authored_timeline(ws):
    call(ws, "canvas_new", {"output": "short-01", "duration_s": 1})
    c = load_working(ws, "short-01")
    data = c.dump()
    for invalid in (0, -1, 1.5, True):
        with pytest.raises(ValidationError):
            Canvas.model_validate({**data, "duration_frames": invalid})
    mixed = Canvas.model_validate({**data, "time_map": [{"media": "src1@missing", "src_in": 0, "src_out": 30}]})
    assert any(p.where == "duration_frames" and p.severity == "error" for p in check(ws, mixed))
    with_source = Canvas.model_validate({**data, "layers": [{"id": "source", "type": "video", "source": "src1@missing", "box": [0, 0, 64, 96]}]})
    assert any("require a source time_map" in p.why for p in check(ws, with_source))
    with pytest.raises(ValueError, match="No source video"):
        from vwb.canvas.copydeck import mint
        mint(ws, copy_id="fake-ocr", role="title", source="ocr", tracks=[1])


def test_cli_and_followup_without_source_skip_video_index_and_keep_plugin_context(tmp_path, monkeypatch):
    from vwb.workspace import request
    monkeypatch.setattr(request, "RUNS_DIR", tmp_path)
    seen = []
    monkeypatch.setattr(orchestrator, "build_index", lambda *args, **kwargs: pytest.fail("No video should be indexed"))
    monkeypatch.setattr(orchestrator.job_lead, "run", lambda workspace, message, first_turn: seen.append((workspace, message, first_turn)) or "Ready")
    monkeypatch.setattr(orchestrator, "ensure_review", lambda workspace: [])
    context = {"plugins": ["figma_direct"]}
    result = orchestrator.chat("Make a 30 second video from this design", context=context)
    workspace = Workspace.open(result["request"])
    assert not workspace.load().sources and workspace.load().chat[0].context["plugins"] == ["figma_direct"]
    assert "no source video" in _opening(workspace, "Make it").lower()
    assert "duration_s" in _opening(workspace, "Make it")
    assert "No source video" in wake_context(workspace)
    workspace.add_chat("job_lead", "Ready")
    orchestrator.chat("Change the title", request_id=workspace.id)
    assert len(seen) == 2 and not seen[1][2]
    assert Path(result["review_page"]).exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="Local ffmpeg required")
def test_real_source_free_render_and_review_without_models_or_dummy_media(ws, monkeypatch):
    profile = load_profile("youtube_shorts")
    ref, _ = image_reference(ws, (profile["width"], profile["height"]))
    made = call(ws, "canvas_new", {"output": "short-01", "duration_s": 0.2})
    path = ws.work / made.data["path"]
    doc = read_json(path)
    doc["layers"] = [{"id": "design", "type": "image", "asset": ref,
                      "box": [0, 0, profile["width"], profile["height"]]}]
    write_json(path, doc)
    c, _ = CanvasStore(ws.canvas).commit(ws, "short-01", "test")
    receipt = pipeline.render(ws, "short-01")
    media = probe(receipt["file"], with_keyframes=False)
    assert media.n_frames == 6 and media.has_audio
    assert (media.width, media.height) == (profile["width"], profile["height"])
    assert receipt["audio"]["mode"] == "silent" and receipt["inputs"]["media"] == {}
    assert not ws.load().sources and not list(ws.media.iterdir())
    monkeypatch.setattr("vwb.workspace.ledger.control_key", lambda: b"test-source-free-review-key")
    report = verify(ws, c.output)
    assert report["attested"]
    qdir = ws.qc / "short-01/v1"
    assert (qdir / "manifest.json").exists() and (qdir / "thumbnail.jpg").exists()
    assert "without a source-video timeline" in (qdir / "description.md").read_text()
