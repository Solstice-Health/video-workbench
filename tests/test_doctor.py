"""Local setup diagnostics stay bounded and never install, start, or call models."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vwb.cli import app


@pytest.fixture
def environment(monkeypatch, tmp_path):
    from vwb import config
    from vwb.browser import runtime
    from vwb.render import plates

    renderer = tmp_path / "remotion"
    (renderer / "node_modules/remotion").mkdir(parents=True)
    (renderer / "node_modules/remotion/package.json").write_text("{}")
    (renderer / "node_modules/@remotion/renderer").mkdir(parents=True)
    fonts = renderer / "public/fonts"
    fonts.mkdir(parents=True)
    (fonts / "example.woff2").write_bytes(b"font")
    (fonts / "manifest.json").write_text(json.dumps([{"faces": [{"file": "example.woff2"}]}]))
    monkeypatch.setattr(plates, "REMOTION_DIR", renderer)
    monkeypatch.setattr(config, "find_env_file", lambda: None)
    keys = SimpleNamespace(openai=None, anthropic=None, google=None, openrouter=None)
    monkeypatch.setattr(config, "secrets", lambda: keys)
    monkeypatch.setattr(config, "slot", lambda name: {"model": "test-opus", "host": "anthropic_api"})
    monkeypatch.setattr(runtime, "status", lambda: {"installed": False})
    monkeypatch.delenv("VWB_BROWSER_CDP_URL", raising=False)
    state = SimpleNamespace(missing=set(), node_version="v22.0.0", npm_version="10.0.0",
                            docker=False, docker_timeout=False, render_browser=True, calls=[],
                            fonts=fonts, keys=keys)

    def which(name):
        if name in state.missing:
            return None
        if name in {"ffmpeg", "ffprobe", "node", "npm"} or name == "docker" and state.docker:
            return f"/fake/{name}"
        return None

    def run(command, **kwargs):
        state.calls.append(command)
        name = Path(command[0]).name
        expected = {"capture_output": True, "text": True, "timeout": 5, "check": False}
        if name == "node" and command[1] == "--eval":
            expected["cwd"] = renderer
        assert kwargs == expected
        if name == "node" and command[1] == "--version":
            output = state.node_version
        elif name == "npm":
            output = state.npm_version
        elif name == "node" and command[1] == "--eval":
            assert "onBrowserDownload:()=>{throw Error(" in command[2]
            return subprocess.CompletedProcess(command, 0 if state.render_browser else 1,
                                               stdout="ready" if state.render_browser else "", stderr="")
        elif name == "docker":
            assert command[1] in {"info", "image"}
            if state.docker_timeout:
                raise subprocess.TimeoutExpired(command, timeout=5)
            output = "29.0.0" if command[1] == "info" else "sha256:test-image"
        else:
            pytest.fail(f"Unexpected diagnostic command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    return state


def test_doctor_allows_ui_without_optional_keys_docker_or_browser(environment):
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Anthropic API key configured" in result.output
    assert "optional" in result.output
    assert "test-opus via anthropic_api" in result.output
    assert "local Claude login" not in result.output
    assert all(Path(command[0]).name != "docker" for command in environment.calls)


@pytest.mark.parametrize("missing", ["ffmpeg", "ffprobe", "node", "npm"])
def test_doctor_fails_for_missing_core_binary(environment, missing):
    environment.missing.add(missing)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "missing" in result.output


def test_doctor_rejects_old_node_and_missing_cached_render_browser(environment):
    environment.node_version = "v14.21.0"
    environment.render_browser = False
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "at least Node 16" in result.output
    assert "npx remotion browser ensure" in result.output


def test_doctor_verifies_font_files_instead_of_directory_presence(environment):
    (environment.fonts / "example.woff2").unlink()
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "npm run setup-fonts" in result.output


def test_doctor_bounds_docker_probe_and_never_prints_provider_keys(environment):
    environment.docker = True
    environment.docker_timeout = True
    environment.keys.anthropic = "private-test-value-never-print"
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "private-test-value" not in result.output
    assert len([command for command in environment.calls if Path(command[0]).name == "docker"]) == 1


def test_doctor_inspects_image_without_starting_containers(environment):
    environment.docker = True
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    commands = [command for command in environment.calls if Path(command[0]).name == "docker"]
    assert commands[0][1:3] == ["info", "--format"]
    assert commands[1][1:4] == ["image", "inspect", "vwb-codex-agent:0.162.1"]


def test_ui_only_needs_no_media_or_node_dependencies(environment):
    environment.missing.update({"ffmpeg", "ffprobe", "node", "npm"})
    (environment.fonts / "manifest.json").unlink()
    result = CliRunner().invoke(app, ["doctor", "--ui-only"])
    assert result.exit_code == 0, result.output
    assert "Bundled workbench UI" in result.output
    assert "optional" in result.output
    assert not environment.calls
