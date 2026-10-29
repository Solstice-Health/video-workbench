"""Owned renderer shutdown, including real descendants in separate POSIX sessions."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from vwb.render import plates

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process identity and sessions")

TREE = r'''
import os, signal, subprocess, sys, time
from pathlib import Path
role, mode, directory = sys.argv[1:]
base = Path(directory)
child = None
if role != 'grandchild':
    next_role = 'child' if role == 'root' else 'grandchild'
    child = subprocess.Popen([sys.executable, __file__, next_role, mode, directory],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL if mode == 'parent-exits' else None,
                             stderr=subprocess.DEVNULL if mode == 'parent-exits' else None)
    while not (base / next_role).exists():
        time.sleep(.01)
def graceful(*args):
    if child:
        child.terminate()
        child.wait(timeout=2)
    (base / (role + '.closed')).write_text('yes')
    sys.exit(0)
if mode == 'graceful':
    signal.signal(signal.SIGTERM, graceful)
elif mode != 'parent-exits' or role != 'root':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
(base / role).write_text(str(os.getpid()))
while True:
    time.sleep(.02)
'''


def process_record(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,lstart="], capture_output=True,
                            text=True, timeout=1, env={"PATH": os.defpath, "LC_ALL": "C"})
    fields = result.stdout.split()
    return (fields[0], " ".join(fields[1:])) if result.returncode == 0 and len(fields) == 6 else None


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), "Process lifecycle did not settle"


@pytest.fixture
def renderer_tree(tmp_path):
    script = tmp_path / "tree.py"
    script.write_text(TREE)
    processes, identities = [], {}

    def launch(mode):
        # Fail visibly if the host blocks ps; these tests must exercise real
        # ownership discovery rather than silently skipping descendant cleanup.
        assert process_record(os.getpid()), "ps access is required for process lifecycle tests"
        process = subprocess.Popen([sys.executable, str(script), "root", mode, str(tmp_path)],
                                   start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(process)
        wait_for(lambda: (tmp_path / "root").exists() and bool((tmp_path / "root").read_text()))
        pids = {role: int((tmp_path / role).read_text()) for role in ("root", "child", "grandchild")}
        identities.update({pid: process_record(pid)[1] for pid in pids.values()})
        assert os.getpgid(pids["child"]) == pids["child"] != process.pid
        assert os.getpgid(pids["grandchild"]) == pids["grandchild"] != pids["child"]
        return process, pids

    yield launch
    for pid, identity in reversed(list(identities.items())):
        record = process_record(pid)
        if record and record[1] == identity:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for process in processes:
        process.communicate(timeout=3)


@pytest.mark.parametrize("mode", ["stubborn", "parent-exits"])
def test_forced_shutdown_stops_detached_term_ignoring_descendants(renderer_tree, mode):
    process, pids = renderer_tree(mode)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        started = time.monotonic()
        plates._stop_renderer(process)
        assert time.monotonic() - started < 21
        assert process.returncode is not None
        for pid in (pids["child"], pids["grandchild"]):
            # On Linux an orphan can remain a zombie until PID 1 reaps it.
            wait_for(lambda pid=pid: (record := process_record(pid)) is None or record[0].startswith("Z"))
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


def test_graceful_shutdown_allows_adapter_to_reap_its_children(renderer_tree, tmp_path, monkeypatch):
    process, _ = renderer_tree("graceful")
    real_kill, real_killpg = os.kill, os.killpg

    def kill(pid, sig):
        assert sig != signal.SIGKILL, "Graceful adapter must not need forced cleanup"
        return real_kill(pid, sig)

    def killpg(pid, sig):
        assert sig != signal.SIGKILL, "Graceful adapter must not need forced group cleanup"
        return real_killpg(pid, sig)

    with monkeypatch.context() as scoped:
        scoped.setattr(plates.os, "kill", kill)
        scoped.setattr(plates.os, "killpg", killpg)
        started = time.monotonic()
        plates._stop_renderer(process)
    assert time.monotonic() - started < 4
    assert process.returncode == 0
    for role in ("root", "child", "grandchild"):
        assert (tmp_path / f"{role}.closed").read_text() == "yes"


@pytest.mark.parametrize("reused", [True, False])
def test_descendants_are_stopped_deepest_first_and_reused_pid_is_spared(monkeypatch, reused):
    parent, child, grandchild = 510001, 510002, 510003
    original = "Thu Oct 29 12:00:00 2026"
    replaced = "Thu Oct 29 12:01:00 2026"
    killed, groups = [], []
    process = SimpleNamespace(pid=parent, returncode=None)
    process.poll = lambda: process.returncode

    def communicate(timeout):
        if process.returncode is None:
            process.returncode = -signal.SIGTERM
            raise subprocess.TimeoutExpired("renderer", timeout)
        return b"", b""

    process.communicate = communicate

    def ps(command, **kwargs):
        assert kwargs["timeout"] <= 1
        assert kwargs["env"] == {"PATH": os.defpath, "LC_ALL": "C"}
        if command[1] == "-axo":
            text = (f"{parent} 1 {original}\n{child} {parent} {original}\n"
                    f"{grandchild} {child} {original}\n999999 1 {original}\n"
                    f"-2 {parent} {original}\n0 {parent} {original}\ninvalid\n")
        else:
            assert command[:2] == ["ps", "-p"]
            pid = int(command[2])
            text = f"{pid} 1 {replaced if reused and pid == child else original}\n"
        return SimpleNamespace(returncode=0, stdout=text)

    monkeypatch.setattr(plates.subprocess, "run", ps)
    monkeypatch.setattr(plates.os, "getpgid", lambda pid: parent)
    monkeypatch.setattr(plates.os, "killpg", lambda pid, sig: groups.append((pid, sig)))
    monkeypatch.setattr(plates.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    plates._stop_renderer(process)
    assert groups == [(parent, signal.SIGTERM)]  # Reaped parent's group is never reused.
    assert killed == [(grandchild, signal.SIGKILL)] + ([] if reused else [(child, signal.SIGKILL)])


def test_unavailable_ps_and_inherited_pipes_do_not_block_cleanup(monkeypatch):
    calls, closed = [], []
    process = SimpleNamespace(pid=510001, returncode=None, stdin=None,
                              stdout=SimpleNamespace(close=lambda: closed.append("stdout")),
                              stderr=SimpleNamespace(close=lambda: closed.append("stderr")))
    process.poll = lambda: process.returncode

    def communicate(timeout):
        calls.append(timeout)
        raise subprocess.TimeoutExpired("renderer", timeout)

    process.communicate = communicate
    process.wait = lambda timeout: setattr(process, "returncode", -signal.SIGKILL)
    monkeypatch.setattr(plates.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("ps unavailable")))
    monkeypatch.setattr(plates.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(plates.os, "killpg", lambda *a: None)
    monkeypatch.setattr(plates.os, "kill", lambda *a: pytest.fail("No descendant identity was verified"))
    plates._stop_renderer(process)
    assert len(calls) == 2
    assert 10 < calls[0] <= 12  # Leave room for Node's 5s close + 5s kill.
    assert 0 < calls[1] <= 2
    assert closed == ["stdout", "stderr"]
