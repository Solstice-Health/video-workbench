"""Renderer selection must preserve editable data, version identity and cache isolation."""
import json
import os

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
def ws(tmp_path, monkeypatch):
    from vwb.render import environment
    details = {"test_runtime": "pinned"}
    monkeypatch.setattr(environment, "capture", lambda: {"sha256": sha256_json(details), "details": details})
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


def test_opaque_browser_png_is_normalized_without_losing_pixels(tmp_path, monkeypatch):
    props = plan(scene('browser'))[0].props
    def execute(stage, jobs, timeout):
        for job in jobs:
            Image.new('RGB', (64, 48), (16, 32, 48)).save(stage / f"{job['pid']}_f0.png")
    monkeypatch.setattr(plates, '_execute', execute)
    got = plates.render_stills(tmp_path, [('Scene', props, [0])])
    with Image.open(next(iter(got.values()))[0]) as output:
        assert output.mode == 'RGBA'
        assert output.getpixel((0, 0)) == (16, 32, 48, 255)


def test_non_alpha_movie_never_enters_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace
    props = plan(scene('browser'))[0].props
    def execute(stage, jobs, timeout):
        for job in jobs:
            from pathlib import Path
            Path(job['out']).write_bytes(b'not published')
    monkeypatch.setattr(plates, '_execute', execute)
    monkeypatch.setattr(plates, 'probe', lambda *a, **k: SimpleNamespace(
        n_frames=props['durationInFrames'], width=64, height=48, pix_fmt='gray', vcodec='rawvideo', fps=24))
    with pytest.raises(ToolError, match='without alpha'):
        plates.render(tmp_path, [('Scene', props)])
    assert not list(tmp_path.glob('*.mov'))
    assert not list(tmp_path.glob('.plates-*'))


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process-group lifecycle')
def test_timed_out_adapter_cleans_its_encoder_and_gets_no_vendor_environment(tmp_path, monkeypatch):
    import os
    import sys
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'browser-render.mjs').write_text('''import json,os,signal,subprocess,sys,time
from pathlib import Path
folder=Path(sys.argv[1]).parent
assert 'VWB_TEST_PRIVATE_CREDENTIAL' not in os.environ
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
(folder/'child.json').write_text(json.dumps({'pid':child.pid}))
def stop(*args):
    child.terminate()
    child.wait(timeout=2)
    (folder/'closed').write_text('yes')
    sys.exit(0)
signal.signal(signal.SIGTERM,stop)
while True: time.sleep(.05)
''')
    monkeypatch.setattr(plates, 'REMOTION_DIR', runtime)
    monkeypatch.setattr(plates, 'ensure_installed', lambda renderer: None)
    monkeypatch.setattr(plates, 'which', lambda name: sys.executable)
    monkeypatch.setenv('VWB_TEST_PRIVATE_CREDENTIAL', 'dummy-only')
    with pytest.raises(ToolError, match='exceeded'):
        plates._execute(tmp_path, [{'renderer': 'browser'}], timeout=1)
    assert (tmp_path / 'closed').read_text() == 'yes'
    pid = json.loads((tmp_path / 'child.json').read_text())['pid']
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_environment_pins_affect_cache_and_block_changed_runtime(ws, monkeypatch):
    from vwb.render import environment
    from vwb.render.pipeline import ensure_plates
    made = call(ws, 'canvas_new', {'output': 'card', 'duration_s': 1, 'renderer': 'browser'})
    path = ws.work / made.data['path']
    raw = read_json(path)
    raw['layers'] = scene().dump()['layers']
    raw['renderer_environment'] = {'sha256': 'agent-claim', 'details': {}}
    write_json(path, raw)
    c, _ = CanvasStore(ws.canvas).commit(ws, 'card', 'test')
    assert c.renderer_environment['sha256'] != 'agent-claim'
    pid = plan(c)[0].plate_id
    assert plan(c)[0].props['__environment'] == c.renderer_environment['sha256']
    replacement = {'details': {'test_runtime': 'changed'}, 'sha256': sha256_json({'test_runtime': 'changed'})}
    monkeypatch.setattr(environment, 'capture', lambda: replacement)
    monkeypatch.setattr(plates, 'render', lambda *a: pytest.fail('Changed runtime must fail before rendering'))
    with pytest.raises(ToolError, match='environment changed'):
        ensure_plates(ws, [c])
    c2, _ = CanvasStore(ws.canvas).commit(ws, 'card', 'test')
    assert c2.renderer_environment == replacement
    assert plan(c2)[0].plate_id != pid
    assert CanvasStore(ws.canvas).get('card', 1).renderer_environment != replacement


def test_media_only_canvas_never_requires_browser_environment(ws, monkeypatch):
    from vwb.render import environment
    monkeypatch.setattr(environment, 'capture', lambda: pytest.fail('Empty/media-only canvas does not use the renderer'))
    made = call(ws, 'canvas_new', {'output': 'card', 'duration_s': 1, 'renderer': 'browser'})
    assert not made.is_error
    c, _ = CanvasStore(ws.canvas).commit(ws, 'card', 'test')
    assert c.renderer_environment is None
    assert plan(c) == []
