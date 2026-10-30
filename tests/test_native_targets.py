"""Native authoring selection must not pretend to be a lossless canvas swap."""
import json
import plistlib
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from vwb.canvas.model import Canvas
from vwb.canvas.renderers import RENDERERS, after_effects_target, catalog, get_renderer
from vwb.desktop import after_effects as ae
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.web.server import Handler
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


def test_catalog_exposes_native_target_without_changing_renderer_contract(monkeypatch):
    registry = build_registry()
    monkeypatch.setattr(ae, '_discover', lambda _: pytest.fail('Catalog must not probe the host'))
    monkeypatch.setattr(ae.subprocess, 'Popen', lambda *a, **k: pytest.fail('Catalog must not launch apps'))
    result = catalog()
    assert result['default'] == 'remotion'
    assert result['renderers'] == [asdict(row) for row in RENDERERS.values()]
    assert list(RENDERERS) == ['remotion', 'browser', 'gsap', 'canvas2d']
    assert [row['id'] for row in result['targets']] == [*RENDERERS, 'after_effects']
    for row in result['targets'][:-1]:
        assert row['kind'] == 'canvas_renderer' and row['supports_renderer_swap']
        assert row['plate_layers'] == RENDERERS[row['id']].plate_layers
    native = result['targets'][-1]
    assert native['kind'] == 'native_project' and not native['supports_renderer_swap']
    assert native['capability'] == 'desktop.after_effects.execute'
    assert native['availability']['status'] == 'unknown'
    assert set(native['required_tools'] + native['import_tools']) <= registry.tools.keys()
    assert all(step['tool'] in registry.tools for step in native['workflow'])
    assert native['workflow'][-1]['tool'] == 'media_register'
    assert 'No automatic' in native['scope']
    # Callers cannot mutate shared catalog state, including legacy plate lists.
    native['required_tools'].clear()
    result['targets'][0]['scope'] = 'corrupted'
    assert catalog()['targets'][-1]['required_tools']
    assert catalog()['targets'][0]['scope'] != 'corrupted'


@pytest.mark.parametrize('changes,expected', [
    ({'supported': False}, 'unavailable'),
    ({'available': False}, 'unavailable'),
    ({'enabled': False}, 'setup_required'),
    ({'can_render': False}, 'setup_required'),
    ({'pending_execution': {'message': 'Inspect the previous script.'}}, 'unavailable'),
    ({}, 'available'),
])
def test_native_availability_is_configuration_not_execution(changes, expected):
    status = {'supported': True, 'available': True, 'enabled': True, 'can_render': True, **changes}
    before = dict(status)
    row = catalog(after_effects_status=status)['targets'][-1]
    assert row['availability']['status'] == expected
    assert row == after_effects_target(status)
    assert status == before
    if expected == 'available':
        assert 'verified only by execution' in row['availability']['reason']


def test_native_target_cannot_be_selected_as_a_canvas_renderer(tmp_path):
    ws = Workspace(tmp_path / 'r-native')
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message='Keep existing canvas'))
    ctx = ToolContext(ws, 'job_lead', 'job_lead', None)
    registry = build_registry()
    made = registry.call(ctx, 'canvas_new', {'output': 'existing', 'duration_s': 1})
    assert not made.is_error
    path = ws.work / made.data['path']
    before = path.read_bytes()
    result = registry.call(ctx, 'canvas_set_renderer', {'output': 'existing', 'renderer': 'after_effects'})
    assert result.is_error and 'native project' in result.text
    assert path.read_bytes() == before
    rejected = registry.call(ctx, 'canvas_new', {'output': 'native', 'duration_s': 1, 'renderer': 'after_effects'})
    assert rejected.is_error and not (ws.work / 'outputs/native/canvas.json').exists()
    raw = json.loads(before)
    raw['renderer'] = 'after_effects'
    with pytest.raises(ValidationError):
        Canvas.model_validate(raw)
    with pytest.raises(ValueError, match='media_register'):
        get_renderer('after_effects')


@pytest.mark.parametrize('kind,status,path,offered', [
    ('render', 'completed', 'movie.mov', True),
    ('render', 'completed', 'movie.MP4', True),
    ('render', 'failed', 'partial.mp4', False),
    ('render', 'running', 'partial.mp4', False),
    ('script', 'completed', 'movie.mov', False),
    ('render', 'completed', 'still.png', False),
    ('render', 'completed', 'sound.wav', False),
    ('render', 'completed', 'other.avi', False),
    ('render', 'completed', 'other.mxf', False),
])
def test_only_completed_importable_movies_offer_a_separate_import(kind, status, path, offered):
    result = ae._public({'kind': kind, 'status': status, 'output': {'path': path}, 'nonce': 'private'})
    assert result['authoring_target'] == 'after_effects'
    assert result['review']['status'] == 'not_evaluated'
    assert 'nonce' not in result
    assert ('import_hint' in result) is offered
    if offered:
        assert result['import_hint']['arguments'] == {'path': path}
        assert 'Registry' in result['import_hint']['note']


@pytest.fixture
def catalog_host(tmp_path, monkeypatch):
    """Configured synthetic installation, distinct from the global default host."""
    runs = tmp_path / 'catalog-host'
    root = runs / '.desktop/after-effects'
    root.mkdir(parents=True)
    app = tmp_path / 'Adobe After Effects.app'
    (app / 'Contents').mkdir(parents=True)
    with (app / 'Contents/Info.plist').open('wb') as handle:
        plistlib.dump({'CFBundleIdentifier': 'com.adobe.AfterEffects', 'CFBundleShortVersionString': '26.0'}, handle)
    renderer = app.parent / 'aerender'
    renderer.write_text('#!/bin/sh\nexit 99\n')
    renderer.chmod(0o755)
    settings = root / 'settings.json'
    settings.write_text(json.dumps({'enabled': True, 'app_path': str(app)}))
    monkeypatch.setattr(ae.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(ae.config, 'RUNS_DIR', tmp_path / 'default-host')
    return SimpleNamespace(runs=runs, root=root, settings=settings, app=app, renderer=renderer)


def route_catalog(runs, *, headers=None):
    """Exercise the real GET handler and origin check without opening a socket."""
    handler = object.__new__(Handler)
    handler.server = SimpleNamespace(workbench=SimpleNamespace(runs_dir=runs), server_address=('127.0.0.1', 12345))
    handler.command = 'GET'
    handler.path = '/api/canvas-renderers'
    handler.headers = {'Host': '127.0.0.1:12345', **(headers or {})}
    result = []
    handler._json = lambda value: result.append(value)
    handler._route()
    return result[0]


def host_snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes() if path.is_file() else None)
            for path in [root, *root.rglob('*')]}


@pytest.mark.parametrize('state,expected', [
    ('configured', 'available'),
    ('disabled', 'setup_required'),
    ('missing_app', 'unavailable'),
    ('missing_renderer', 'setup_required'),
    ('pending', 'unavailable'),
])
def test_live_catalog_route_and_registry_tool_share_read_only_host_readiness(catalog_host, tmp_path, monkeypatch, state, expected):
    host = catalog_host
    registry = build_registry()  # Load dependencies before forbidding processes.
    from vwb.web import server
    if state == 'disabled':
        host.settings.write_text(json.dumps({'enabled': False, 'app_path': str(host.app)}))
    elif state == 'missing_app':
        (host.app / 'Contents/Info.plist').unlink()
    elif state == 'missing_renderer':
        host.renderer.unlink()
    elif state == 'pending':
        (host.root / 'active.json').write_text(json.dumps({'job_id': 'a' * 32, 'job_file': str(host.root / 'missing-job.json')}))
    before = host_snapshot(host.runs)
    ws = Workspace(tmp_path / 'request')
    ws.work.mkdir(parents=True)
    ws.ledger.mkdir()
    ctx = ToolContext(ws, 'lead', 'job_lead', None,
                      state={'integration_service': SimpleNamespace(after_effects=ae.AfterEffects(host.runs))})

    def forbidden(*args, **kwargs):
        pytest.fail('Catalog reads must not launch processes, initialize integrations or configure AE')

    monkeypatch.setattr(ae.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(ae, 'configure', forbidden)
    monkeypatch.setattr(ae, '_write_json', forbidden)
    monkeypatch.setattr(server, 'IntegrationService', forbidden)
    for _ in range(2):
        routed = route_catalog(host.runs)
        tool = registry.call(ctx, 'canvas_renderers', {})
        assert not tool.is_error, tool.text
        assert routed == tool.data
        target = next(row for row in routed['targets'] if row['id'] == 'after_effects')
        assert target['availability']['status'] == expected
        assert routed['renderers'] == catalog()['renderers']
    assert host_snapshot(host.runs) == before
    assert not ae.config.RUNS_DIR.exists()  # Both paths honor the supplied host.
    with pytest.raises(PermissionError, match='Cross-site'):
        route_catalog(host.runs, headers={'Sec-Fetch-Site': 'cross-site'})


def test_catalog_without_injected_service_probes_default_host_without_creating_state(tmp_path, monkeypatch):
    from vwb.tools.canvas_tools import canvas_renderers

    runs = tmp_path / 'never-configured'
    monkeypatch.setattr(ae.config, 'RUNS_DIR', runs)
    monkeypatch.setattr(ae.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(ae, '_discover', lambda settings: (None, None))
    monkeypatch.setattr(ae.subprocess, 'Popen', lambda *a, **k: pytest.fail('Catalog cannot launch an app'))
    ctx = SimpleNamespace(state={})
    for result in (canvas_renderers(ctx, {}).data, route_catalog(runs)):
        assert result['targets'][-1]['availability']['status'] == 'unavailable'
    assert not runs.exists()
