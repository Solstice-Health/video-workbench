"""Resource portability and state paths for editable checkouts and installed wheels."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from vwb import config
from vwb.browser import runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installed(tmp_path):
    target = tmp_path / "installed"
    shutil.copytree(ROOT / "src/vwb", target / "vwb", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for source, destination in metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"].items():
        source, destination = ROOT / source, target / destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    return target


def test_source_checkout_paths_remain_unchanged():
    assert config.SOURCE_CHECKOUT
    assert config.REPO_ROOT == ROOT and config.RESOURCE_ROOT == ROOT
    assert config.CONFIG_DIR == ROOT / "config"
    assert config.GENERATORS_DIR == ROOT / "generators"
    assert config.DOCS_DIR == ROOT / "docs"


def test_browser_runtime_uses_configured_runs_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "custom-runs")
    assert runtime.runtime_dir() == tmp_path / "custom-runs/.browser-runtime"


def test_installed_package_reads_bundled_resources_and_writes_to_launch_directory(installed, tmp_path):
    launch = tmp_path / "project"
    launch.mkdir()
    env_file = launch / ".env"
    env_file.write_text("# Deliberately empty test environment\n")
    env = {key: value for key, value in os.environ.items() if key not in {"VWB_ENV_FILE", "VWB_RUNS_DIR", "PYTHONPATH"}
           and not key.endswith(("_API_KEY", "_TOKEN"))}
    env["PYTHONPATH"] = str(installed)
    code = '''
import json
from pathlib import Path
from vwb import config, prompts
from vwb.agents import job_lead
from vwb.canvas.model import font_library
from vwb.tools import build_registry
from vwb.browser.runtime import runtime_dir
assert not config.SOURCE_CHECKOUT
assert config.REPO_ROOT == Path.cwd()
assert config.RUNS_DIR == Path.cwd() / "runs"
assert config.find_env_file() == Path.cwd() / ".env"
assert config.CONFIG_DIR.is_relative_to(config.PACKAGE_DIR)
assert config.slot("main")["model"]
assert config.load_profile("youtube_shorts")
assert len(prompts.PROFILES) == 6 and len(font_library()) >= 16
assert all(path.is_file() for path in job_lead._docs().values())
assert build_registry().for_role("job_lead")
assert runtime_dir() == config.RUNS_DIR / ".browser-runtime"
print(json.dumps({"resources": str(config.RESOURCE_ROOT), "runs": str(config.RUNS_DIR)}))
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=launch, env=env, capture_output=True, text=True, check=True)
    paths = json.loads(result.stdout)
    assert Path(paths["resources"]) == installed / "vwb/_resources"
    assert Path(paths["runs"]) == launch / "runs"
    assert not (installed / "runs").exists()


def test_runtime_resource_manifest_contains_every_font_and_license():
    base = config.GENERATORS_DIR / "remotion/public/fonts"
    entries = json.loads((base / "manifest.json").read_text())
    for entry in entries:
        assert (base / entry["license_file"]).is_file(), entry["family"]
        for face in entry["faces"]:
            assert (base / face["file"]).is_file(), face["file"]
