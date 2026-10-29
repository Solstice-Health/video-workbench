"""Renderer selection must preserve editable data, version identity and cache isolation."""
import json

import pytest
from PIL import Image
from pydantic import ValidationError

from vwb.canvas.compile import PLATE_PIN, check, load_working, plan
from vwb.canvas.model import Canvas
from vwb.canvas.renderers import RENDERERS, get_renderer
from vwb.canvas.store import CanvasStore
from vwb.render import plates
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import read_json, write_json
from vwb.util.procs import ToolError
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path):
    ws = Workspace(tmp_path / "r-adapters")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="Design a card"))
    return ws


def call(ws, tool, args, role="job_lead"):
    return build_registry().call(ToolContext(ws, role, role, None), tool, args)


def scene(renderer=None):
    return Canvas.model_validate({"request": "r-adapters", "output": "card", "format": {"width": 64, "height": 48, "fps": 24},
                                  "time_map": [], "duration_frames": 6,
                                  **({"renderer": renderer} if renderer else {}),
                                  "layers": [{"id": "box", "type": "shape", "shape": "rect", "box": [0, 0, 32, 32],
                                              "fill": "#FF3300", "span": [2, 6]}]})


def test_legacy_canvas_and_plate_identity_unchanged():
    c = scene()
    step = plan(c)[0]
    assert c.renderer == "remotion" and c.renderer_pin is None
    assert "__renderer" not in step.props
    assert PLATE_PIN == "remotion@4.0.534:Scene@4"
    assert step.plate_id == sha256_json({"pin": PLATE_PIN, "props": step.props})
    assert step.plate_id == plates.plate_id("Scene", step.props)
    assert (step.start, step.end, step.props["offset"]) == (2, 6, 2)


def test_renderer_changes_only_backend_and_cache_identity():
    scenes = [scene(renderer) for renderer in RENDERERS]
    assert len({plan(c)[0].plate_id for c in scenes}) == len(RENDERERS)
    for c in scenes:
        step = plan(c)[0]
        assert step.plate_id == plates.plate_id("Scene", step.props)
        assert c.layers == scenes[0].layers and c.length == 6
        assert step.props["offset"] == 2 and step.props["durationInFrames"] == 4
    with pytest.raises(ValidationError):
        scene("unknown")
    with pytest.raises(ValueError, match="trusted Scene"):
        plates.plate_id("../arbitrary", plan(scenes[0])[0].props)


def test_swap_preserves_layers_and_old_versions(ws):
    created = call(ws, "canvas_new", {"output": "card", "duration_s": 1})
    path = ws.work / created.data["path"]
    raw = read_json(path)
    raw.update(layers=scene().dump()["layers"], intent="Preserve this", declared={"custom": "claim"})
    write_json(path, raw)
    first, _ = CanvasStore(ws.canvas).commit(ws, "card", "test")
    first_bytes = CanvasStore(ws.canvas).path("card", 1).read_bytes()
    assert first.renderer_pin == get_renderer("remotion").pin
    swapped = call(ws, "canvas_set_renderer", {"output": "card", "renderer": "browser"})
    assert not swapped.is_error
    changed = read_json(path)
    assert {k: v for k, v in changed.items() if k not in {"renderer", "renderer_pin"}} == {
        k: v for k, v in raw.items() if k not in {"renderer", "renderer_pin"}}
    second, _ = CanvasStore(ws.canvas).commit(ws, "card", "test")
    assert (second.version, second.parent, second.renderer) == (2, 1, "browser")
    assert second.renderer_pin == get_renderer("browser").pin
    assert CanvasStore(ws.canvas).path("card", 1).read_bytes() == first_bytes
    assert first.provenance["sha256"] != second.provenance["sha256"]


def test_unsupported_swap_is_atomic_and_pins_fail_closed(ws):
    made = call(ws, "canvas_new", {"output": "card", "duration_s": 1, "renderer": "browser"})
    path = ws.work / made.data["path"]
    doc = read_json(path)
    doc["layers"] = [{"id": "title", "type": "text", "box": [0, 0, 100, 100], "copy_id": "title",
                      "style": {"font": "Inter", "weight": 700, "size": 24, "color": "#FFFFFF"}}]
    write_json(path, doc)
    before = path.read_bytes()
    result = call(ws, "canvas_set_renderer", {"output": "card", "renderer": "canvas2d"})
    assert result.is_error and "does not support text" in result.text
    assert path.read_bytes() == before
    c = scene("browser")
    c.renderer_pin = "browser-scene@unavailable"
    assert any(p.where == "renderer_pin" and p.severity == "error" for p in check(ws, c))
    with pytest.raises(ValueError, match="pins"):
        plan(c)


def test_renderer_tools_obey_registry_role_gates(ws):
    assert not call(ws, "canvas_renderers", {}, "critic").is_error
    assert call(ws, "canvas_set_renderer", {"output": "card", "renderer": "browser"}, "critic").is_error
    assert not call(ws, "canvas_new", {"output": "card", "duration_s": 1, "renderer": "gsap"}, "composer").is_error
    assert load_working(ws, "card").renderer == "gsap"
    assert call(ws, "canvas_new", {"output": "bad", "duration_s": 1, "renderer": "bad"}).is_error
    assert not (ws.work / "outputs/bad/canvas.json").exists()


def test_empty_and_cached_previews_do_not_require_renderer_install(tmp_path, monkeypatch):
    monkeypatch.setattr(plates, "ensure_installed", lambda *a: pytest.fail("No install required"))
    assert plates.render(tmp_path, []) == []
    assert plates.render_stills(tmp_path, []) == {}
    props = plan(scene("browser"))[0].props
    pid = plates.plate_id("Scene", props)
    (tmp_path / "stills").mkdir()
    Image.new("RGBA", (64, 48)).save(tmp_path / "stills" / f"{pid}_f0.png")
    assert plates.render_stills(tmp_path, [("Scene", props, [0])])[pid][0].is_file()


def test_missing_still_and_wrong_size_fail_without_cache_poison(tmp_path, monkeypatch):
    props = plan(scene("browser"))[0].props
    pid = plates.plate_id("Scene", props)
    monkeypatch.setattr(plates, "_execute", lambda *a: None)
    with pytest.raises(ToolError, match="no valid still"):
        plates.render_stills(tmp_path, [("Scene", props, [0])])
    assert not (tmp_path / "stills" / f"{pid}_f0.png").exists()

    def wrong_size(stage, jobs, timeout):
        for job in jobs:
            Image.new("RGBA", (1, 1)).save(stage / f"{job['pid']}_f0.png")
    monkeypatch.setattr(plates, "_execute", wrong_size)
    with pytest.raises(ToolError, match="incorrect size"):
        plates.render_stills(tmp_path, [("Scene", props, [0])])
    assert not list(tmp_path.glob(".stills-*"))
    assert not (tmp_path / "stills" / f"{pid}_f0.png").exists()


def test_dispatch_preserves_backend_and_unions_duplicate_frames(tmp_path, monkeypatch):
    seen = []
    def execute(stage, jobs, timeout):
        seen.extend(json.loads(json.dumps(jobs)))
        for job in jobs:
            for frame in job["stills"]:
                Image.new("RGBA", (64, 48)).save(stage / f"{job['pid']}_f{frame}.png")
    monkeypatch.setattr(plates, "_execute", execute)
    props = plan(scene("gsap"))[0].props
    got = plates.render_stills(tmp_path, [("Scene", props, [0]), ("Scene", props, [2])])
    assert seen[0]["renderer"] == "gsap" and seen[0]["stills"] == [0, 2]
    assert set(next(iter(got.values()))) == {0, 2}
    with pytest.raises(ValueError, match="within the local timeline"):
        plates.render_stills(tmp_path, [("Scene", props, [-1])])
