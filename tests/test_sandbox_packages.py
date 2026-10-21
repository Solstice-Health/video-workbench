"""Public-network isolation, reusable environments and background job contracts."""
from __future__ import annotations

import io
import json
import socket
import time
from types import SimpleNamespace

import pytest

from vwb.sandbox import egress, jobs
from vwb.tools import sandbox_tools as sandbox
from vwb.tools.base import Registry, ToolContext, ToolResult


@pytest.fixture
def ctx(tmp_path):
    ws = SimpleNamespace(root=tmp_path, work=tmp_path / 'work', ledger=tmp_path / 'ledger')
    ws.work.mkdir()
    ws.ledger.mkdir()
    return ToolContext(ws=ws, agent='job_lead', role='job_lead', gateway=None)


def call(ctx, name, args):
    return Registry(sandbox.TOOLS).call(ctx, name, args)


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.1.2.3', '172.17.0.1', '192.168.1.1', '169.254.169.254',
                                   '0.0.0.0', '224.0.0.1', '100.64.0.1', '::1', 'fc00::1', 'fe80::1',
                                   '::ffff:8.8.8.8', '2002:0808:0808::1', '64:ff9b::c0a8:101',
                                   '168.63.129.16'])
def test_proxy_blocks_local_reserved_and_transition_addresses(monkeypatch, ip):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (ip, 443))])
    with pytest.raises(ValueError):
        egress.public_addresses('example.test', 443)


def test_mixed_public_private_dns_answer_is_denied(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('8.8.8.8', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError):
        egress.public_addresses('example.test', 443)


def test_public_connect_pins_dns_address(monkeypatch):
    seen = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('8.8.8.8', 443))])
    connection = SimpleNamespace(settimeout=lambda _: None, connect=lambda address: seen.append(address))
    monkeypatch.setattr(socket, 'socket', lambda *a: connection)
    assert egress.connect_public('public.example', 443) is connection
    assert seen == [('8.8.8.8', 443)]


def test_public_command_retains_network_none_and_no_host_socket(ctx):
    argv = sandbox._command(ctx, ['curl', 'https://example.com'], '/workspace', 20, 'test', {'network': 'public'})
    assert '--network=none' in argv
    assert '--interactive' in argv
    assert '/run/vwb-network.py' in argv
    assert not any('docker.sock' in value or 'host.docker.internal' in value for value in argv)
    assert argv[-2:] == ['curl', 'https://example.com']
    assert not any('KEY=' in value or 'TOKEN=' in value for value in argv)


def test_network_policy_blocks_even_when_execution_allowed(ctx, monkeypatch):
    monkeypatch.setattr(Registry, 'mode', staticmethod(lambda cap: 'block' if cap == 'sandbox.network' else 'allow'))
    monkeypatch.setattr(sandbox, '_dispatch', lambda *a: pytest.fail('must not run'))
    result = call(ctx, 'sandbox_run', {'argv': ['curl', 'https://example.com'], 'network': 'public'})
    assert result.is_error and 'sandbox.network' in result.text


def test_environment_uses_literal_requirements_and_persistent_interpreter(ctx, monkeypatch):
    seen = []
    monkeypatch.setattr(sandbox, '_dispatch', lambda ctx, args: seen.append(args) or ToolResult.json({'status': 'completed'}))
    result = call(ctx, 'sandbox_environment', {'name': 'pdf', 'packages': ['pypdf==6.0.0', 'https://example.com/p.whl']})
    assert not result.is_error
    assert result.data['python'] == '/workspace/.sandbox/envs/pdf/bin/python'
    assert seen[0]['network'] == 'public'
    assert json.loads(seen[0]['argv'][-1]) == ['pypdf==6.0.0', 'https://example.com/p.whl']
    assert 'requirements.freeze.txt' in seen[0]['argv'][2]


@pytest.mark.parametrize('args', [{'name': '../escape'}, {'packages': ['--index-url=bad']}, {'packages': 'pypdf'},
                                  {'packages': ['bad\0package']}, {'name': ''}])
def test_environment_rejects_paths_and_pip_options(ctx, args):
    assert call(ctx, 'sandbox_environment', args).is_error


def test_environment_honors_execution_policy(ctx, monkeypatch):
    monkeypatch.setattr(Registry, 'mode', staticmethod(lambda cap: 'block' if cap == 'sandbox.run' else 'allow'))
    result = call(ctx, 'sandbox_environment', {'name': 'default'})
    assert result.is_error and 'sandbox.run' in result.text


def test_capture_drains_large_output_but_retains_bounded_log():
    log = io.BytesIO()
    capture = sandbox._Capture(io.BytesIO(b'x' * (sandbox.LOG_BYTES + 100)), log)
    capture.text()
    assert capture.total == sandbox.LOG_BYTES + 100
    assert len(log.getvalue()) == sandbox.LOG_BYTES
    assert len(capture.data) == sandbox.OUTPUT_BYTES


def make_job(ctx, **changes):
    path = jobs._root(ctx.ws) / ('a' * 32)
    path.mkdir()
    row = {'id': path.name, 'owner': ctx.agent, 'status': 'running', 'heartbeat': time.time(), 'created': time.time(),
           'request_root': str(ctx.ws.root), 'arguments': {'argv': ['python3']}, **changes}
    (path / 'job.json').write_text(json.dumps(row))
    return path


def test_jobs_page_output_and_cancel_without_replay(ctx):
    path = make_job(ctx)
    (path / 'stdout.log').write_bytes(b'a' * 20_000)
    first = jobs.status(ctx, {'job_id': path.name})
    assert len(first['stdout']) == 16_000 and first['stdout_more']
    second = jobs.status(ctx, {'job_id': path.name, 'stdout_offset': first['stdout_next_offset'], 'cancel': True})
    assert len(second['stdout']) == 4000
    assert second['stdout_next_offset'] == 20_000 and not second['stdout_more']
    assert second['cancel_requested'] and (path / 'cancel').exists()
    assert 'request_root' not in second and 'arguments' not in second


def test_stale_supervisor_is_interrupted_and_never_replayed(ctx, monkeypatch):
    path = make_job(ctx, heartbeat=time.time() - 100)
    removed = []
    monkeypatch.setattr(sandbox, '_remove', lambda name: removed.append(name))
    result = jobs.status(ctx, {'job_id': path.name})
    assert result['status'] == 'interrupted'
    assert removed == ['vwb-sandbox-' + path.name]
    assert (path / 'cancel').exists()


def test_background_worker_environment_has_no_provider_secrets(ctx, monkeypatch):
    observed = []
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-leak')
    monkeypatch.setenv('VWB_ENV_FILE', '/secret/env')
    monkeypatch.setattr(jobs.subprocess, 'Popen', lambda command, **kw: observed.append((command, kw)) or SimpleNamespace(pid=123))
    result = jobs.launch(ctx, {'argv': ['python3', '-c', 'print(1)'], 'background': True})
    assert result['status'] == 'starting'
    command, options = observed[0]
    assert command[1:3] == ['-m', 'vwb.sandbox.worker']
    assert not any(key in options['env'] for key in ('OPENAI_API_KEY', 'VWB_ENV_FILE'))
    assert options['start_new_session'] and not options.get('shell')


@pytest.mark.parametrize('args', [{'job_id': '../escape'}, {'cancel': True}, {'wait_s': 31},
                                  {'wait_s': True}, {'stdout_offset': -1}, {'stderr_offset': '1'}])
def test_job_control_validation(ctx, args):
    with pytest.raises(ValueError):
        jobs.status(ctx, args)


def test_network_ask_parks_before_execution(ctx, monkeypatch):
    from vwb.ask.broker import Broker
    monkeypatch.setattr(Broker, '__init__', lambda *a, **k: None)
    monkeypatch.setattr(Registry, 'mode', staticmethod(lambda cap: 'ask' if cap == 'sandbox.network' else 'allow'))
    monkeypatch.setattr(Broker, 'ask', lambda *a, **k: {'id': 'permission-1', 'status': 'open'})
    monkeypatch.setattr(sandbox, '_dispatch', lambda *a: pytest.fail('must remain parked'))
    result = call(ctx, 'sandbox_run', {'argv': ['curl', 'https://example.com'], 'network': 'public'})
    assert result.data['parked'] and result.data['ticket'] == 'permission-1'


def test_background_concurrency_is_bounded(ctx, monkeypatch):
    for index in range(4):
        path = jobs._root(ctx.ws) / (str(index) * 32)
        path.mkdir()
        (path / 'job.json').write_text(json.dumps({'id': path.name, 'status': 'running', 'heartbeat': time.time()}))
    monkeypatch.setattr(jobs.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not launch a fifth worker'))
    with pytest.raises(ValueError, match='Four sandbox jobs'):
        jobs.launch(ctx, {'argv': ['python3'], 'background': True})


def test_read_only_status_does_not_require_command_permission(ctx, monkeypatch):
    path = make_job(ctx, status='completed')
    monkeypatch.setattr(Registry, 'mode', staticmethod(lambda cap: 'block' if cap == 'sandbox.run' else 'allow'))
    result = call(ctx, 'sandbox_status', {'job_id': path.name})
    assert not result.is_error and result.data['status'] == 'completed'


def test_job_terminal_state_survives_late_running_or_completion_update(ctx):
    path = make_job(ctx, status='interrupted', note='Supervisor stopped', ended=10)
    jobs.update(path, status='running', heartbeat=time.time())
    row = jobs.update(path, status='completed', ended=20, note='Command completed', exit_code=0)
    assert row['status'] == 'interrupted' and row['ended'] == 10
    assert row['note'] == 'Supervisor stopped'
    assert row['exit_code'] == 0  # later diagnostic details are still retained


def test_concurrent_job_writers_preserve_each_others_fields(ctx):
    import concurrent.futures
    path = make_job(ctx)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda index: jobs.update(path, **{f'field_{index}': index}), range(100)))
    row = jobs._read(path)
    assert all(row[f'field_{index}'] == index for index in range(100))


def test_named_environment_refuses_concurrent_installation(ctx, monkeypatch, tmp_path):
    import fcntl
    import subprocess
    import sys
    observed = []
    monkeypatch.setattr(sandbox, '_dispatch', lambda ctx, args: observed.append(args) or ToolResult.json({'status': 'completed'}))
    call(ctx, 'sandbox_environment', {'name': 'shared'})
    script = observed[0]['argv'][2]
    environment = tmp_path / 'envs/shared'
    environment.parent.mkdir()
    with (environment.parent / '.shared.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, '-c', script, str(environment), '[]'], capture_output=True, text=True)
    assert result.returncode == 2 and 'environment is busy' in result.stderr
    assert not environment.exists()


class FakeSocket:
    def __init__(self, chunks=()):
        self.chunks = iter(chunks)
        self.sent = bytearray()
        self.closed = False

    def recv(self, size):
        return next(self.chunks, b'')

    def sendall(self, value):
        self.sent.extend(value)

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True


def handle_proxy(request):
    import threading
    server = SimpleNamespace(slots=threading.BoundedSemaphore(1), stop=threading.Event())
    egress.Handler(request, None, server)


def test_http_proxy_rewrites_authority_and_strips_proxy_credentials(monkeypatch):
    request = FakeSocket([b'GET http://example.com/a?q=b HTTP/1.1\r\nHost: localhost\r\n'
                          b'Proxy-Authorization: secret\r\nAccept: text/plain\r\n\r\n'])
    upstream = FakeSocket([b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok'])
    connected = []
    monkeypatch.setattr(egress, 'connect_public', lambda host, port: connected.append((host, port)) or upstream)
    handle_proxy(request)
    assert connected == [('example.com', 80)]
    assert b'Host: example.com\r\n' in upstream.sent
    assert b'secret' not in upstream.sent and b'localhost' not in upstream.sent
    assert b'200 OK' in request.sent and upstream.closed


@pytest.mark.parametrize('headers', [b'Transfer-Encoding: chunked\r\n',
                                     b'Content-Length: 1\r\nContent-Length: 1\r\n'])
def test_http_proxy_rejects_ambiguous_request_framing(monkeypatch, headers):
    request = FakeSocket([b'POST http://example.com/ HTTP/1.1\r\n' + headers + b'\r\nx'])
    monkeypatch.setattr(egress, 'connect_public', lambda *a: pytest.fail('must reject before upstream connection'))
    handle_proxy(request)
    assert b'403' in request.sent


def test_proxy_transport_separates_protocol_from_script_output():
    import base64

    from vwb.sandbox.transport import PublicProcess
    def frame(kind, data):
        return json.dumps({'kind': kind, 'channel': 0, 'data': base64.b64encode(data).decode()}).encode() + b'\n'
    fake = SimpleNamespace(stdout=io.BytesIO(frame('stdout', b'hello\n') + frame('stderr', b'child error\n')),
                           stderr=io.BytesIO(b'docker diagnostic\n'), stdin=io.BytesIO(), returncode=0,
                           poll=lambda: 0, wait=lambda timeout=None: 0)
    process = PublicProcess(fake)
    out, err = sandbox._Capture(process.stdout), sandbox._Capture(process.stderr)
    process.wait(3)
    assert out.text() == 'hello\n'
    errors = err.text()
    assert 'child error' in errors and 'docker diagnostic' in errors
    assert 'channel' not in errors
