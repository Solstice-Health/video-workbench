"""Setup must not replace dependencies beneath a live Workbench process."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from filelock import FileLock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup.sh"


def test_setup_refuses_a_running_server_before_touching_dependencies(tmp_path):
    project = tmp_path / "checkout"
    (project / "scripts").mkdir(parents=True)
    script = project / "scripts" / "setup.sh"
    shutil.copy2(SCRIPT, script)
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").symlink_to(sys.executable)
    runs = tmp_path / "persistent-runs"
    runs.mkdir()
    with FileLock(runs / ".web-server.lock"):
        result = subprocess.run(["bash", str(script), "--ui-only"],
                                env={**os.environ, "VWB_RUNS_DIR": str(runs)},
                                text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert "No dependencies were changed" in result.stderr
    assert not (project / ".env").exists()


def test_setup_help_is_available_before_any_dependencies():
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "--ui-only" in result.stdout


@pytest.mark.parametrize("ui_only", [False, True])
def test_setup_modes_complete_on_macos_bash_with_no_optional_flags(tmp_path, ui_only):
    project = tmp_path / "checkout"
    (project / "scripts").mkdir(parents=True)
    script = project / "scripts/setup.sh"
    shutil.copy2(SCRIPT, script)
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands.log"
    recorder = '#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$VWB_SETUP_TEST_LOG"\n'
    for name in ("uv", "node", "npm", "ffmpeg", "ffprobe"):
        (tools / name).write_text(recorder)
        (tools / name).chmod(0o700)
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    cli = python.parent / "vwb"
    cli.write_text(recorder)
    cli.chmod(0o700)
    remotion = project / "generators/remotion/node_modules/.bin/remotion"
    remotion.parent.mkdir(parents=True)
    remotion.write_text(recorder)
    remotion.chmod(0o700)
    result = subprocess.run(["bash", str(script), *(["--ui-only"] if ui_only else [])],
                            env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
                                 "VWB_RUNS_DIR": str(tmp_path / "runs"), "VWB_ENV_FILE": str(tmp_path / "empty.env"),
                                 "VWB_SETUP_TEST_LOG": str(log)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    commands = log.read_text().splitlines()
    assert commands[0] == "uv sync --frozen"
    assert commands[-1] == ("vwb doctor --ui-only" if ui_only else "vwb doctor")
    assert any(line.startswith("npm ci ") for line in commands) != ui_only
