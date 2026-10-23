"""Actual Docker lifecycle/isolation acceptance; explicitly enabled in CI."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vwb.code import runtime

pytestmark = pytest.mark.skipif(os.environ.get("VWB_CODE_SERVICE_INTEGRATION") != "1",
                                reason="Opt-in real Docker lifecycle acceptance")


def test_real_worker_is_warm_across_clients_and_owned_shutdown_cleans_it(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "example.py").write_text("answer = 42\n")
    source = Path(runtime.__file__).resolve().parents[2]
    socket_path = runtime.endpoint(work)
    assert not runtime._healthy(socket_path)
    launcher = "import sys;sys.path.insert(0,sys.argv.pop(1));from vwb.code.service import main;main()"
    supervisor = subprocess.Popen([sys.executable, "-I", "-c", launcher, str(source), str(work), str(socket_path)],
                                   env=runtime.clean_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    docker = shutil.which("docker")
    assert docker
    owned_names = []
    try:
        until = time.monotonic() + 8
        while not runtime._healthy(socket_path) and time.monotonic() < until:
            assert supervisor.poll() is None
            time.sleep(0.05)
        assert runtime._healthy(socket_path)
        first = runtime.invoke(work, "status")
        # A second host process must connect to the same worker, like the next
        # provider/turn, rather than merely reusing a Python singleton.
        script = ("import sys,json;sys.path.insert(0,sys.argv[1]);"
                  "from pathlib import Path;from vwb.code.runtime import invoke;"
                  "print(json.dumps(invoke(Path(sys.argv[2]),'status')))" )
        second = subprocess.run([sys.executable, "-I", "-c", script, str(source), str(work)],
                                env=runtime.clean_environment(), capture_output=True, text=True, timeout=20, check=True)
        assert json.loads(second.stdout)["worker_instance"] == first["worker_instance"]
        assert runtime.invoke(work, "read", {"path": "example.py"})["items"][0]["text"] == "answer = 42"
        ids = subprocess.run([docker, "ps", "-q", "--filter", "label=vwb.code-tools=1"],
                             capture_output=True, text=True, check=True, timeout=10).stdout.split()
        for container in ids:
            details = json.loads(subprocess.run([docker, "inspect", container], capture_output=True, text=True,
                                                check=True, timeout=10).stdout)[0]
            if not any(item.get("Destination") == "/workspace" and item.get("Source") == str(work.resolve())
                       for item in details["Mounts"]):
                continue
            owned_names.append(details["Name"].lstrip("/"))
            assert details["HostConfig"]["NetworkMode"] == "none"
            assert details["HostConfig"]["ReadonlyRootfs"] is True
            assert details["Config"]["User"] == "1001:1001"
            assert all(not mount["RW"] for mount in details["Mounts"] if mount["Type"] == "bind")
            names = {entry.split("=", 1)[0] for entry in details["Config"]["Env"]}
            assert not names & {"OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"}
        assert len(owned_names) == 1
        supervisor.terminate()
        supervisor.wait(timeout=12)
        assert not socket_path.exists()
        for name in owned_names:
            result = subprocess.run([docker, "inspect", name], capture_output=True, timeout=10)
            assert result.returncode != 0
        # Recovery is an explicit fresh read, never replay of a prior operation.
        recovered = runtime.invoke(work, "status")
        assert recovered["worker_instance"] != first["worker_instance"]
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=12)
        if supervisor.stderr:
            supervisor.stderr.close()
        runtime.stop(work)
