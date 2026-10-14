from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from vwb.browser import runtime
from vwb.browser.driver import AgentBrowserDriver, BrowserDriverError, ProcessOutput, subprocess_runner


class StubRunner:
    def __init__(self):
        self.calls = []
        self.reply = {"success": True, "data": {}}
        self.returncode = 0
        self.stderr = b""
        self.hook = None

    async def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.hook:
            self.hook(argv)
        return ProcessOutput(self.returncode, json.dumps(self.reply).encode(), self.stderr)


@pytest.fixture
def setup_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "socket_directory", lambda private, session: runtime.private_directory(tmp_path / "sockets"))
    runner = StubRunner()
    driver = AgentBrowserDriver(tmp_path / ".browser", "b123", binary=tmp_path / "agent-browser", runner=runner)
    return driver, runner


def launch(driver, mode="headless"):
    return asyncio.run(driver.start(mode))


def test_private_launch_config_and_environment(setup_driver, monkeypatch):
    driver, runner = setup_driver
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leave-host")
    monkeypatch.setenv("AGENT_BROWSER_PROFILE", "Default")
    monkeypatch.setenv("AGENT_BROWSER_CDP", "ws://wrong-host/")
    monkeypatch.setenv("AGENT_BROWSER_CONFIG", "/home/other/config.json")
    monkeypatch.setenv("DISPLAY", ":22")
    launch(driver)
    config = json.loads(driver.config_path.read_text())
    assert config["profile"] == str(driver.private_dir / "profile")
    assert config["pinTab"] is True and config["autoConnect"] is False
    assert config["headed"] is False and config["allowFileAccess"] is False
    assert stat.S_IMODE(driver.config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(driver.private_dir.stat().st_mode) == 0o700
    for argv, kwargs in runner.calls:
        assert argv[1:6] == ["--config", str(driver.config_path), "--session", "b123", "--json"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "AGENT_BROWSER_PROFILE" not in kwargs["env"]
        assert "AGENT_BROWSER_CDP" not in kwargs["env"]
        assert "AGENT_BROWSER_CONFIG" not in kwargs["env"]
        assert kwargs["env"]["DISPLAY"] == ":22"
        assert kwargs["cwd"] == driver.private_dir
    assert runner.calls[-1][0][-4:] == ["set", "viewport", "1280", "800"]


def test_remote_endpoint_stays_out_of_argv_config_and_results(setup_driver):
    driver, runner = setup_driver
    endpoint = "wss://user:very-secret@browser.example/cdp?token=private-key"
    driver.remote_cdp = endpoint
    runner.reply = {"success": True, "data": {"message": endpoint, "cdpUrl": endpoint}}
    result = launch(driver, "remote")
    assert "very-secret" not in json.dumps(result) and "private-key" not in json.dumps(result)
    assert "profile" not in json.loads(driver.config_path.read_text())
    assert endpoint not in driver.config_path.read_text()
    assert runner.calls[0][0][-3:] == ["tab", "new", "about:blank"]
    for argv, kwargs in runner.calls:
        assert endpoint not in argv
        assert kwargs["env"]["AGENT_BROWSER_CDP"] == endpoint


@pytest.mark.parametrize("argument", ["--session", "--config=/tmp/escape.json", "--profile", "--cdp", "--headed",
                                      "--user-agent", "--device", "--no-ca-cert", "--annotate", "--help", "-V",
                                      "--screenshot-quality", "--future-global-option", "-s"])
def test_operation_args_cannot_reconfigure_driver(setup_driver, argument):
    driver, runner = setup_driver
    launch(driver)
    before = len(runner.calls)
    with pytest.raises(ValueError, match="override"):
        asyncio.run(driver.run(["fill", "@e1", argument]))
    assert len(runner.calls) == before


def test_operation_flags_are_allowed_only_in_known_option_positions(setup_driver):
    driver, runner = setup_driver
    launch(driver)
    for operation in (["snapshot", "-s", "--profile"], ["dialog", "accept", "--annotate"],
                      ["scroll", "down", "500", "--selector", "--help"], ["screenshot", "--full"]):
        before = len(runner.calls)
        with pytest.raises(ValueError, match="override"):
            asyncio.run(driver.run(operation))
        assert len(runner.calls) == before
    asyncio.run(driver.run(["scroll", "down", "500", "--selector", "@e1"]))
    assert runner.calls[-1][0][-2:] == ["--selector", "@e1"]


def test_close_waits_for_owned_daemon_to_exit_without_replaying(setup_driver, monkeypatch):
    driver, runner = setup_driver
    launch(driver)
    monkeypatch.setattr(driver, "alive", Mock(side_effect=[True, True, False]))
    before = len(runner.calls)
    asyncio.run(driver.stop())
    assert len(runner.calls) == before + 1
    assert runner.calls[-1][0][-1] == "close"


def test_mapped_args_are_not_shell_code(setup_driver):
    driver, runner = setup_driver
    launch(driver)
    literal = 'a; $(touch /tmp/nope) `echo secret` "quoted"\nline2'
    asyncio.run(driver.run(["fill", "@e1", literal]))
    assert runner.calls[-1][0][-1] == literal


def test_missing_binary_does_not_launch_or_write_config(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "socket_directory", lambda private, session: tmp_path / "sockets")
    monkeypatch.setattr(runtime, "binary_path", Mock(side_effect=FileNotFoundError(runtime.INSTALL_HINT)))
    runner = StubRunner()
    driver = AgentBrowserDriver(tmp_path / ".browser", "abc", runner=runner)
    with pytest.raises(BrowserDriverError, match="runtime install"):
        launch(driver)
    assert not runner.calls and not driver.config_path.exists()


def test_failure_redacts_endpoint_and_has_code(setup_driver):
    driver, runner = setup_driver
    launch(driver)
    driver.remote_cdp = "wss://host.example?token=provider-secret"
    runner.reply = {"success": False, "code": "tab_gone", "error": "Lost wss://host.example?token=provider-secret Bearer other-secret"}
    with pytest.raises(BrowserDriverError) as error:
        asyncio.run(driver.run(["click", "@e8"]))
    assert error.value.code == "tab_gone"
    assert "provider-secret" not in str(error.value) and "other-secret" not in str(error.value)


def test_invalid_json_is_bounded_and_does_not_echo_stdout(setup_driver):
    driver, _ = setup_driver
    launch(driver)

    async def broken(*args, **kwargs):
        return ProcessOutput(2, b"secret output", b"token=secret-value " + b"x" * 3000)

    driver.runner = broken
    with pytest.raises(BrowserDriverError) as error:
        asyncio.run(driver.run(["get", "url"]))
    assert "secret output" not in str(error.value) and "secret-value" not in str(error.value)
    assert len(str(error.value)) < 1100


def test_timeout_is_not_retried(setup_driver):
    driver, runner = setup_driver
    launch(driver)
    calls = []

    async def timeout(*args, **kwargs):
        calls.append(args)
        raise TimeoutError

    driver.runner = timeout
    with pytest.raises(BrowserDriverError, match="effect is unknown") as error:
        asyncio.run(driver.run(["click", "@e1"]))
    assert error.value.code == "timeout" and len(calls) == 1


def test_stop_and_stream_metadata_do_not_start_daemon(setup_driver):
    driver, runner = setup_driver
    assert asyncio.run(driver.stop()) == {"closed": True, "was_running": False}
    assert asyncio.run(driver.stream_url()) is None
    (driver.socket_dir / "b123.stream").write_text("12345")
    assert asyncio.run(driver.stream_url()) == "ws://127.0.0.1:12345/?pacing=ack&maxFps=15"
    (driver.socket_dir / "b123.stream").write_text("70000")
    assert asyncio.run(driver.stream_port()) is None
    assert not runner.calls


def test_alive_checks_exact_binary_and_socket_without_cli(setup_driver, monkeypatch):
    driver, runner = setup_driver
    temp = tempfile.TemporaryDirectory(prefix="vwb-test-", dir="/tmp")
    driver.socket_dir = Path(temp.name)
    (driver.socket_dir / "b123.pid").write_text(str(os.getpid()))
    endpoint = driver.socket_dir / "b123.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(endpoint))
        server.listen(1)
        monkeypatch.setattr("vwb.browser.driver.subprocess.run", lambda *a, **kw: Mock(stdout=str(driver.binary)))
        assert driver.alive()
        monkeypatch.setattr("vwb.browser.driver.subprocess.run", lambda *a, **kw: Mock(stdout="/another/agent-browser"))
        assert not driver.alive()
    temp.cleanup()
    assert not runner.calls


def test_screenshot_reads_only_private_generated_path_and_removes_it(setup_driver):
    driver, runner = setup_driver
    launch(driver)
    result = io.BytesIO()
    Image.new("RGB", (12, 8), "white").save(result, format="PNG")

    def capture(argv):
        index = argv.index("screenshot")
        Path(argv[index + 1]).write_bytes(result.getvalue())

    runner.hook = capture
    runner.reply = {"success": True, "data": {"path": "/etc/passwd", "annotations": [{"ref": "e1", "box": {"x": 1}}]}}
    shot = asyncio.run(driver.screenshot(annotate=True))
    assert shot.data == result.getvalue() and (shot.width, shot.height) == (12, 8)
    assert shot.mime_type == "image/png" and shot.annotations[0]["ref"] == "e1"
    assert not list((driver.private_dir / "screenshots").iterdir())


def test_screenshot_rejects_symlink_without_reading_target(setup_driver, tmp_path):
    driver, runner = setup_driver
    launch(driver)
    secret = tmp_path / "private.txt"
    secret.write_text("do not return")
    runner.hook = lambda argv: Path(argv[argv.index("screenshot") + 1]).symlink_to(secret)
    with pytest.raises(BrowserDriverError, match="valid bounded screenshot"):
        asyncio.run(driver.screenshot())
    assert secret.read_text() == "do not return"


def test_bbox_labels_frame_local_uncertainty(setup_driver):
    driver, runner = setup_driver
    launch(driver)
    runner.reply = {"success": True, "data": {"x": 10, "y": 20, "width": 30, "height": 40}}
    assert asyncio.run(driver.bbox("@e1"))["reliable_for_viewport"] is False


def test_snapshot_and_tabs_use_current_cli_schema(setup_driver):
    driver, runner = setup_driver
    launch(driver)
    runner.reply = {"success": True, "data": {"snapshot": "- button Go [ref=e4]", "refs": {"e4": {"role": "button", "name": "Go"}}}}
    assert asyncio.run(driver.snapshot(interactive=True, compact=True, depth=3))["refs"]["e4"]["name"] == "Go"
    assert runner.calls[-1][0][-5:] == ["snapshot", "-i", "-c", "-d", "3"]
    runner.reply = {"success": True, "data": {"tabs": [{"tabId": "t2", "targetId": "real-target", "url": "https://example.com", "active": True}]}}
    assert asyncio.run(driver.tabs())[0]["id"] == "t2"


def test_socket_path_short_even_for_deep_request_and_rejects_symlink(tmp_path, monkeypatch):
    # Use the real uid-owned short-root helper. No daemon is started.
    path = runtime.socket_directory(tmp_path / ("long" * 35) / ".browser", "b123")
    assert len(os.fsencode(path / "b123.sock")) < 104
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    path.rmdir()
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        runtime.private_directory(link)


def test_runtime_checksum_failure_prevents_execution(tmp_path, monkeypatch):
    executable = tmp_path / "agent-browser"
    executable.write_bytes(b"wrong executable")
    executable.chmod(0o700)
    monkeypatch.setattr(runtime, "platform_key", lambda: "darwin-arm64")
    with pytest.raises(RuntimeError, match="checksum"):
        runtime.binary_path(tmp_path)


def test_installer_keeps_existing_binary_on_bad_download(tmp_path, monkeypatch):
    existing = tmp_path / "agent-browser"
    existing.write_bytes(b"existing")
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"bad download"))
    with pytest.raises(RuntimeError, match="checksum"):
        runtime.install(tmp_path)
    assert existing.read_bytes() == b"existing"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["agent-browser"]


def test_real_subprocess_timeout_reaps_cli_and_does_not_repeat(tmp_path):
    marker = tmp_path / "pid"
    script = "import os,time,pathlib; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(10)"
    with pytest.raises(TimeoutError):
        asyncio.run(subprocess_runner([sys.executable, "-c", script], env={"PATH": os.environ["PATH"]}, cwd=tmp_path, timeout_s=0.3))
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_runtime_hash_cache_invalidates_on_file_change(tmp_path, monkeypatch):
    executable = tmp_path / "agent-browser"
    executable.write_bytes(b"pinned")
    executable.chmod(0o700)
    expected = runtime.digest(executable)
    monkeypatch.setattr(runtime, "platform_key", lambda: "test-platform")
    monkeypatch.setitem(runtime.SHA256, "test-platform", expected)
    original = runtime.digest
    hashed = Mock(side_effect=original)
    monkeypatch.setattr(runtime, "digest", hashed)
    runtime.binary_path(tmp_path)
    runtime.binary_path(tmp_path)
    assert hashed.call_count == 1
    executable.write_bytes(b"modified binary")
    with pytest.raises(RuntimeError, match="checksum"):
        runtime.binary_path(tmp_path)
    assert hashed.call_count == 2


def test_full_snapshot_is_default_and_frame_check_is_conservative(setup_driver):
    driver, runner = setup_driver
    launch(driver)
    asyncio.run(driver.snapshot())
    assert runner.calls[-1][0][-1] == "snapshot"
    for value, expected in [(False, False), (True, True), (None, True), ("false", True)]:
        runner.reply = {"success": True, "data": {"result": value}}
        assert asyncio.run(driver.has_frames()) is expected
