"""Owned tunnel startup, exact origins and lifecycle; no external ngrok calls."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from vwb.web.tunnel import Ngrok

ORIGIN = 'https://fixture.ngrok.app'


def binary(tmp_path, event, *, exit_after=False):
    script = tmp_path / 'ngrok-fixture'
    script.write_text(f'#!{sys.executable}\nimport json,os,pathlib,sys,time\n'
        f'pathlib.Path({str(tmp_path / "args.json")!r}).write_text(json.dumps(sys.argv[1:]))\n'
        f'pathlib.Path({str(tmp_path / "pid")!r}).write_text(str(os.getpid()))\n'
        f'print({json.dumps(event)!r},flush=True)\n' + ('sys.exit(1)\n' if exit_after else 'time.sleep(60)\n'))
    script.chmod(0o700)
    return str(script)


def test_tunnel_keeps_credentials_off_argv_disables_inspection_and_never_pools(tmp_path):
    fake = binary(tmp_path, {'msg': 'started tunnel', 'url': ORIGIN})
    tunnel = Ngrok(8766, ORIGIN, executable=fake)
    try:
        assert tunnel.origin == ORIGIN
        args = json.loads((tmp_path / 'args.json').read_text())
        assert args[:2] == ['http', 'http://127.0.0.1:8766']
        assert '--inspect=false' in args and args[-2:] == ['--url', ORIGIN]
        assert not any('pooling' in arg or 'authtoken' in arg or 'host-header' in arg for arg in args)
        assert tunnel.log.stat().st_mode & 0o777 == 0o600
        assert tunnel.process.poll() is None
    finally:
        tunnel.close()
    assert tunnel.process.poll() is not None and not tunnel.log.exists()


def test_assigned_domain_is_discovered_without_a_reserved_url(tmp_path):
    tunnel = Ngrok(8766, executable=binary(tmp_path, {'msg': 'started tunnel', 'url': ORIGIN}))
    try:
        assert tunnel.origin == ORIGIN
        assert '--url' not in json.loads((tmp_path / 'args.json').read_text())
    finally:
        tunnel.close()


@pytest.mark.parametrize('url', ['https://another.ngrok.app', 'http://fixture.ngrok.app', 'https://fixture.ngrok.app/path'])
def test_invalid_or_mismatched_origin_stops_owned_agent(tmp_path, url):
    with pytest.raises(ValueError):
        Ngrok(8766, ORIGIN, executable=binary(tmp_path, {'msg': 'started tunnel', 'url': url}))
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / 'pid').read_text()), 0)


def test_failure_exposes_only_ngrok_error_code_not_provider_log(tmp_path):
    fake = binary(tmp_path, {'msg': 'secret-fixture-token error ERR_NGROK_334'}, exit_after=True)
    with pytest.raises(ValueError) as error:
        Ngrok(8766, executable=fake)
    assert 'ERR_NGROK_334' in str(error.value)
    assert 'secret-fixture-token' not in str(error.value)


def test_startup_timeout_reaps_agent(tmp_path):
    with pytest.raises(ValueError, match='timed out'):
        Ngrok(8766, timeout=2, executable=binary(tmp_path, {'msg': 'waiting'}))
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / 'pid').read_text()), 0)


@pytest.mark.parametrize('termination', [None, signal.SIGTERM, signal.SIGHUP])
def test_serve_binds_before_tunneling_sets_origin_and_cleans_up(tmp_path, monkeypatch, capsys, termination):
    from vwb.web import server
    events = []
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    monkeypatch.delenv('VWB_UI_PUBLIC_URL', raising=False)
    class FakeServer:
        def __init__(self, address, app):
            self.server_port, self.app = address[1], app
            events.append('bind')
        def serve_forever(self, **kwargs):
            assert self.app.public_access.origin == ORIGIN
            assert os.environ['VWB_UI_PUBLIC_URL'] == ORIGIN
            events.append('serve')
            if termination:
                signal.getsignal(termination)(termination, None)
        def server_close(self):
            events.append('server_close')
    class FakeTunnel:
        def __init__(self, port, origin):
            assert port == 8766 and events == ['bind']
            events.append('tunnel')
            self.origin = ORIGIN
        def close(self):
            events.append('tunnel_close')
    monkeypatch.setattr(server, 'WorkbenchServer', FakeServer)
    monkeypatch.setattr('vwb.web.tunnel.Ngrok', FakeTunnel)
    server.serve(port=8766, open_browser=False, runs_dir=tmp_path, ngrok=True)
    assert events == ['bind', 'tunnel', 'serve', 'tunnel_close', 'server_close']
    assert 'VWB_UI_PUBLIC_URL' not in os.environ
    assert all(signal.getsignal(sig) == handler for sig, handler in original_handlers.items())
    output = capsys.readouterr().out
    assert ORIGIN + '/#access=' in output
    assert ORIGIN + '/api/integrations/solstice/callback' in output


def test_occupied_port_never_starts_tunnel(tmp_path, monkeypatch):
    from vwb.web import server
    def occupied(*args):
        raise OSError('address already in use')
    def forbidden(*args):
        pytest.fail('Cannot tunnel a port owned by another process')
    monkeypatch.setattr(server, 'WorkbenchServer', occupied)
    monkeypatch.setattr('vwb.web.tunnel.Ngrok', forbidden)
    with pytest.raises(OSError, match='address already in use'):
        server.serve(port=8765, open_browser=False, runs_dir=tmp_path, ngrok=True)


@pytest.mark.parametrize('termination', [signal.SIGTERM, signal.SIGHUP, signal.SIGKILL])
def test_tunnel_does_not_outlive_abrupt_parent_exit(tmp_path, termination):
    fake = binary(tmp_path, {'msg': 'started tunnel', 'url': ORIGIN})
    code = f"from vwb.web.tunnel import Ngrok; import time; tunnel = Ngrok(8766, executable={fake!r}); time.sleep(60)"
    owner = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'pid').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        child = int((tmp_path / 'pid').read_text())
        owner.send_signal(termination)
        owner.wait(timeout=5)
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(.05)
        pytest.fail('ngrok child survived its owning Workbench')
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
