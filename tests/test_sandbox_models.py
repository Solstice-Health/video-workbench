"""Scoped model access over the same stdio transport used by Docker."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vwb.sandbox import jobs, model_client, runner, worker
from vwb.sandbox.transport import PublicProcess
from vwb.tools import sandbox_tools as sandbox
from vwb.tools.base import Registry, Tool, ToolContext, ToolResult, schema


@pytest.fixture
def ctx(tmp_path):
    ws = SimpleNamespace(root=tmp_path, work=tmp_path / 'work', ledger=tmp_path / 'ledger')
    ws.work.mkdir()
    ws.ledger.mkdir()
    return ToolContext(ws=ws, agent='limited-child', role='worker', gateway=None)


def test_model_sandbox_command_has_sdk_without_network_or_vendor_secrets(ctx):
    command = sandbox._command(ctx, ['python3', 'batch.py'], '/workspace', 20, 'test', {'models': True})
    assert '--network=none' in command and '--interactive' in command
    assert '--env=PYTHONPATH=/run/vwb-sdk' in command and '--models' in command
    assert any(value.endswith(':/run/vwb-sdk/vwb_models.py:ro') for value in command)
    assert '--public' not in command
    assert not any('KEY=' in value or 'TOKEN=' in value for value in command)


def test_public_and_model_access_are_independent_options(ctx):
    command = sandbox._command(ctx, ['python3', 'batch.py'], '/workspace', 20, 'test',
                               {'network': 'public', 'models': True})
    assert '--public' in command and '--models' in command and '--network=none' in command
    with pytest.raises(ValueError, match='models must be a boolean'):
        sandbox._options({'models': 'true'})


def test_model_dispatch_cannot_expand_inherited_permissions_or_bypass_policy(ctx, monkeypatch):
    observed = []
    tools = [Tool(name, '', schema({}), lambda ctx, args: observed.append(ctx.role) or ToolResult.json(args),
                  'model.call') for name in sandbox.MODEL_TOOLS]
    monkeypatch.setattr('vwb.tools.build_registry', lambda: Registry(tools))
    ctx.state['allowed_tools'] = ['sandbox_run', 'model_catalog']
    handler = sandbox._model_handler(ctx, time.monotonic() + 20)
    denied = handler('model_request', {}, threading.Event())
    assert denied['is_error'] and 'inherited' in denied['text']
    allowed = handler('model_catalog', {'provider': 'openrouter'}, threading.Event())
    assert allowed['data'] == {'provider': 'openrouter'} and observed == ['worker']
    monkeypatch.setattr(Registry, 'mode', staticmethod(lambda capability: 'block'))
    denied = handler('model_catalog', {}, threading.Event())
    assert denied['is_error'] and 'policy dial' in denied['text']


def test_model_dispatch_honors_parent_cancel_and_execution_deadline(ctx, monkeypatch):
    tools = [Tool('model_providers', '', schema({}), lambda *a: pytest.fail('cancelled call ran'))]
    monkeypatch.setattr('vwb.tools.build_registry', lambda: Registry(tools))
    stop = threading.Event()
    stop.set()
    handler = sandbox._model_handler(ctx, time.monotonic() + 20)
    assert 'interrupted' in handler('model_providers', {}, stop)['text']
    handler = sandbox._model_handler(ctx, time.monotonic() - 1)
    assert 'deadline' in handler('model_providers', {}, threading.Event())['text']


def test_stopping_bridge_prevents_subsequent_dispatch(ctx, monkeypatch):
    observed = []
    tool = Tool('model_providers', '', schema({}), lambda *a: observed.append(True) or ToolResult.json({}))
    monkeypatch.setattr('vwb.tools.build_registry', lambda: Registry([tool]))
    stop = threading.Event()
    handler = sandbox._model_handler(ctx, time.monotonic() + 20)
    assert not handler('model_providers', {}, stop)['is_error']
    stop.set()
    assert handler('model_providers', {}, stop)['is_error']
    assert observed == [True]


@pytest.mark.parametrize('public', [False, True])
def test_real_runner_model_round_trip_and_long_payload_without_provider_keys(ctx, tmp_path, public):
    sdk = tmp_path / 'sdk'
    sdk.mkdir()
    (sdk / 'vwb_models.py').write_bytes(Path(model_client.__file__).read_bytes())
    environment = {**os.environ, 'PYTHONPATH': str(sdk)}
    environment.pop('VWB_MODEL_ENDPOINT', None)
    script = """import json,os
from vwb_models import providers,catalog,request
assert bool(os.environ.get('HTTP_PROXY')) == PUBLIC
assert 'OPENAI_API_KEY' not in os.environ
print(json.dumps([providers(),catalog('openrouter',limit=2),request('openrouter','chat',{'text':'x'*200000})]))
""".replace('PUBLIC', repr(public))
    # The real sandbox supplies an allowlisted environment; match that here so
    # host proxy or exported vendor settings cannot affect this fixture.
    for key in list(environment):
        if key.endswith('_KEY') or key.lower().endswith('_proxy'):
            environment.pop(key)
    process = subprocess.Popen([sys.executable, '-I', runner.__file__,
                                *(['--public'] if public else []), '--models', '--', sys.executable, '-c', script],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    calls = []

    def handler(name, args, stop):
        calls.append((name, args))
        return {'is_error': False, 'data': {'tool': name, 'size': len(args.get('payload', {}).get('text', ''))}}

    transport = PublicProcess(process, model_handler=handler, allow_public=public)
    out, err = sandbox._Capture(transport.stdout), sandbox._Capture(transport.stderr)
    assert transport.wait(timeout=15) == 0, err.text()
    results = json.loads(out.text())
    assert [row['tool'] for row in results] == list(sandbox.MODEL_TOOLS)
    assert results[-1]['size'] == 200000
    assert calls[1][1]['limit'] == 2
    assert not err.text()


def test_sdk_requires_an_opted_in_loopback_endpoint(monkeypatch):
    monkeypatch.delenv('VWB_MODEL_ENDPOINT', raising=False)
    with pytest.raises(model_client.ModelError, match='models=true'):
        model_client.providers()
    monkeypatch.setenv('VWB_MODEL_ENDPOINT', 'http://example.com/request')
    with pytest.raises(model_client.ModelError, match='models=true'):
        model_client.providers()


def test_transport_refuses_non_model_tools_before_registry_dispatch():
    transport = object.__new__(PublicProcess)
    transport.model_handler = lambda *args: pytest.fail('arbitrary tool reached the registry')
    transport.model_lock = threading.Lock()
    transport.model_channels = {1}
    transport.model_slots = threading.BoundedSemaphore(4)
    transport.model_slots.acquire()
    transport.stop = threading.Event()
    responses = []
    transport._emit = lambda kind, channel, data: responses.append(json.loads(data))
    transport._model(1, json.dumps({'tool': 'file_read', 'arguments': {'path': '.env'}}).encode())
    assert responses[0]['is_error']


def test_transport_keeps_unicode_receipt_data_under_byte_limit():
    transport = object.__new__(PublicProcess)
    native = {'response': '🟠' * 10000, 'response_path': 'model-output/fixture/response.json'}
    transport.model_handler = lambda *args: {'is_error': False, 'text': json.dumps(native, ensure_ascii=False),
                                             'data': native}
    transport.model_lock = threading.Lock()
    transport.model_channels = {1}
    transport.model_slots = threading.BoundedSemaphore(4)
    transport.model_slots.acquire()
    transport.stop = threading.Event()
    responses = []
    transport._emit = lambda kind, channel, data: responses.append(json.loads(data))
    transport._model(1, json.dumps({'tool': 'model_request', 'arguments': {}}).encode())
    assert responses[0]['data'] == native


def test_sdk_refuses_oversized_payload_before_connecting(monkeypatch):
    monkeypatch.setenv('VWB_MODEL_ENDPOINT', 'http://127.0.0.1:1/request')
    monkeypatch.setattr(model_client, 'MAX_REQUEST_BYTES', 1024)
    with pytest.raises(model_client.ModelError, match='32 MiB'):
        model_client.request('openrouter', 'chat.completions', {'text': 'x' * 2000})


def test_model_job_saves_original_role_and_intersection_privately(ctx, monkeypatch):
    ctx.state['allowed_tools'] = ['sandbox_run', 'model_providers']
    monkeypatch.setenv('VWB_ENV_FILE', '/private/config/models.env')
    monkeypatch.setattr(jobs.subprocess, 'Popen', lambda *a, **kw: SimpleNamespace(pid=123))
    result = jobs.launch(ctx, {'argv': ['python3', 'batch.py'], 'background': True, 'models': True})
    row = json.loads((jobs._root(ctx.ws) / result['id'] / 'job.json').read_text())
    assert row['role'] == 'worker' and row['model_permissions'] == ['model_providers']
    assert row['host_config']['VWB_ENV_FILE'] == '/private/config/models.env'
    assert not any(name in result for name in ('host_config', 'model_permissions', 'arguments', 'request_root'))


def test_model_job_worker_restores_role_instead_of_job_lead(ctx, monkeypatch):
    directory = jobs._root(ctx.ws) / ('a' * 32)
    directory.mkdir()
    path = directory / 'job.json'
    row = {'id': directory.name, 'owner': ctx.agent, 'role': 'worker', 'status': 'starting',
           'request_root': str(ctx.ws.root), 'arguments': {'argv': ['python3'], 'models': True},
           'model_permissions': ['model_catalog'], 'heartbeat': time.time()}
    path.write_text(json.dumps(row))
    observed = []
    monkeypatch.setattr(worker, 'Workspace', lambda root: ctx.ws)
    monkeypatch.setattr(worker, '_execute', lambda restored, *a, **kw:
                        observed.append(restored) or ToolResult.json({'status': 'completed'}))
    monkeypatch.setattr('vwb.gateway.Gateway', lambda *a, **kw: 'host-gateway')
    worker.main(path)
    assert observed[0].role == 'worker' and observed[0].gateway == 'host-gateway'
    assert observed[0].state['allowed_tools'] == ['model_catalog']
