"""Warm supervisor framing, isolation and interruption; no model/API calls."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from vwb.code import runtime, service


def test_docker_mounts_commands_and_environment_are_fixed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "should-never-travel")
    monkeypatch.setenv("NODE_OPTIONS", "--require=untrusted.js")
    command = service.docker_command(tmp_path, "vwb-code-owned-test")
    joined = " ".join(command)
    assert "--network=none" in command and "--read-only" in command and "--user=1001:1001" in command
    assert "target=/workspace,readonly" in joined and "target=/run/vwb_code,readonly" in joined
    assert "docker.sock" not in joined and "should-never-travel" not in joined
    assert "OPENAI_API_KEY" not in runtime.clean_environment() and "NODE_OPTIONS" not in runtime.clean_environment()
    assert command[-5:-2] == ["python3", "-I", "-u"]


def test_transport_rejects_oversize_before_sending():
    class NeverSend:
        def sendall(self, _):
            raise AssertionError("No oversized frame should be sent")
    with pytest.raises(runtime.CodeServiceError, match="byte limit"):
        runtime.send_frame(NeverSend(), {"text": "x" * runtime.MAX_FRAME})


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "image_status", lambda: {"ready": True})
    # The test launches a fixed protocol fixture instead of a Docker client.
    # Container cleanup attempts cannot reach a real daemon in this unit test.
    monkeypatch.setattr(service.shutil, "which", lambda _: "/nonexistent/docker")
    supervisor = service.Supervisor(tmp_path)
    yield supervisor
    supervisor._kill()


def _fixture_command(tmp_path, monkeypatch, body):
    script = tmp_path / "trusted_fixture.py"
    script.write_text(body)
    monkeypatch.setattr(service, "docker_command", lambda *_: [sys.executable, "-I", "-u", str(script)])


def _request(index=1, args=None):
    return {"id": f"{index:032x}", "operation": "status", "arguments": args or {}}


def test_warm_worker_is_reused_and_frames_survive_partial_writes(supervisor, tmp_path, monkeypatch):
    _fixture_command(tmp_path, monkeypatch, """import sys,json,os
for line in sys.stdin:
 request=json.loads(line)
 payload=json.dumps({'id':request['id'],'result':{'pid':os.getpid(),'length':len(request['arguments'].get('text',''))}})+'\\n'
 for part in (payload[:7],payload[7:]):
  sys.stdout.write(part);sys.stdout.flush()
""")
    actual_write = os.write
    monkeypatch.setattr(service.os, "write", lambda fd, data: actual_write(fd, data[:13]))
    first = supervisor.call(_request(args={"text": "x" * 32000}))["result"]
    second = supervisor.call(_request(2))["result"]
    assert first["pid"] == second["pid"] and first["length"] == 32000


def test_nonreading_worker_times_out_even_during_stdin_write(supervisor, tmp_path, monkeypatch):
    _fixture_command(tmp_path, monkeypatch, "import time;time.sleep(60)\n")
    monkeypatch.setattr(service, "CALL_TIMEOUT", 0.1)
    def cannot_write(*_):
        raise BlockingIOError
    monkeypatch.setattr(service.os, "write", cannot_write)
    started = time.monotonic()
    with pytest.raises(runtime.CodeServiceError, match="accept.*time"):
        supervisor.call(_request(args={"text": "x" * 64000}))
    assert time.monotonic() - started < 3
    assert supervisor.process is None


def test_cancel_only_targets_its_own_active_request(supervisor, tmp_path, monkeypatch):
    _fixture_command(tmp_path, monkeypatch, "import sys,time;sys.stdin.readline();time.sleep(60)\n")
    errors = []
    def call():
        try:
            supervisor.call(_request())
        except (runtime.CodeServiceError, OSError, ValueError) as exc:
            errors.append(exc)
    thread = threading.Thread(target=call)
    thread.start()
    until = time.monotonic() + 3
    while supervisor.process is None and time.monotonic() < until:
        time.sleep(0.01)
    process = supervisor.process
    assert process is not None
    supervisor.cancel(f"{2:032x}")
    assert supervisor.process is process
    supervisor.cancel(f"{1:032x}")
    thread.join(timeout=3)
    assert not thread.is_alive() and errors and supervisor.process is None


def test_response_identity_failure_discards_worker(supervisor, tmp_path, monkeypatch):
    _fixture_command(tmp_path, monkeypatch, "import sys,json,time\nfor line in sys.stdin:\n print(json.dumps({'id':'wrong','result':{}}),flush=True)\n")
    with pytest.raises(runtime.CodeServiceError, match="mismatched"):
        supervisor.call(_request())
    assert supervisor.process is None


def test_image_status_is_read_only_and_actionable(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    result = runtime.image_status()
    assert result["ready"] is False and "runtime install" in result["message"]


def test_worker_cannot_run_directly_on_host(monkeypatch):
    from vwb.code import worker
    monkeypatch.setattr(worker.Path, "exists", lambda _: False)
    with pytest.raises(SystemExit, match="fixed Docker image"):
        worker.main()


def test_cold_start_lock_wait_honors_cancellation(tmp_path, monkeypatch):
    from filelock import Timeout
    cancel = threading.Event()
    class BusyLock:
        def __init__(self, *args, **kwargs):
            pass
        def acquire(self, **kwargs):
            cancel.set()
            raise Timeout("busy")
    monkeypatch.setattr(runtime, "FileLock", BusyLock)
    monkeypatch.setattr(runtime, "_healthy", lambda _: False)
    started = time.monotonic()
    with pytest.raises(InterruptedError, match="startup"):
        runtime._ensure(tmp_path, tmp_path / "socket", cancel=cancel)
    assert time.monotonic() - started < 1
