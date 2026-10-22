#!/usr/bin/env python3
"""Exercise a built wheel outside its checkout, without model calls or services.

Uses the current interpreter's installed dependencies, but imports vwb only from
the extracted wheel. This catches missing data files and source-tree path leaks.
CI also runs dependency installation separately through the frozen uv lockfile.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROBE = r'''
import importlib, json, os, runpy
from pathlib import Path
import vwb
from vwb import config, prompts
from vwb.agents import job_lead
from vwb.browser import runtime
from vwb.canvas.model import font_library
from vwb.tools import build_registry
from vwb.web.server import STATIC_DIR
wheel_root = Path(os.environ["VWB_SMOKE_WHEEL_ROOT"]).resolve()
project = Path.cwd().resolve()
assert Path(vwb.__file__).resolve().is_relative_to(wheel_root), vwb.__file__
assert not config.SOURCE_CHECKOUT
assert config.RESOURCE_ROOT.is_relative_to(wheel_root)
assert config.REPO_ROOT == project
assert config.RUNS_DIR == project / "runs"
assert runtime.runtime_dir() == config.RUNS_DIR / ".browser-runtime"
assert config.find_env_file().resolve() == project / ".env"
assert not any(item["configured"] for item in config.provider_status())
assert config.load_config("policy")
assert config.slot("main")["model"]
assert config.load_profile("youtube_shorts")
assert len(prompts.catalog()["profiles"]) == 6
assert len(font_library()) >= 16
for entry in json.loads((config.GENERATORS_DIR / "remotion/public/fonts/manifest.json").read_text()):
    base = config.GENERATORS_DIR / "remotion/public/fonts"
    assert (base / entry["license_file"]).is_file(), entry["family"]
    for face in entry["faces"]:
        assert (base / face["file"]).is_file(), face["file"]
assert (config.GENERATORS_DIR / "remotion/render.mjs").is_file()
assert (config.GENERATORS_DIR / "remotion/package-lock.json").is_file()
assert (config.RESOURCE_ROOT / "docker/codex-agent/Dockerfile").is_file()
assert (STATIC_DIR / "index.html").is_file()
docs = job_lead._docs()
assert all(path.is_file() for path in docs.values())
assert (config.DOCS_DIR / "CANVAS.md").read_text().startswith("# Canvas")
assert (config.DOCS_DIR / "DESKTOP_APPS.md").is_file()
from vwb.integrations import IntegrationService
from vwb.integrations.blender_mcp import LOCK
assert LOCK.is_file() and "mcp-for-blender==2.2.2" in LOCK.read_text()
creative = {row["id"]: row for row in IntegrationService().status()["items"]}
assert not creative["after_effects"]["enabled"]
assert not creative["blender_mcp"]["allow_code_execution"]
assert not creative["blender_mcp"]["prepared"]
assert len(build_registry().for_role("job_lead")) > 30
importlib.import_module("vwb.cli")
frontend_test = os.environ.get("VWB_SMOKE_FRONTEND_TEST")
if frontend_test:
    runpy.run_path(frontend_test, run_name="__main__")
print(json.dumps({"package": str(Path(vwb.__file__).resolve()), "resources": str(config.RESOURCE_ROOT),
                  "profiles": len(prompts.PROFILES), "font_families": len(font_library()),
                  "source_checkout": config.SOURCE_CHECKOUT, "model_calls": 0}))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--frontend-test", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            parts = Path(name).parts
            if Path(name).is_absolute() or any(part in {"..", "node_modules", "runs", ".git", "__pycache__", ".keys"} for part in parts):
                raise RuntimeError(f"Unexpected private/generated path in wheel: {name}")
            if Path(name).name.startswith(".env") or name.endswith(".pyc"):
                raise RuntimeError(f"Unexpected environment/bytecode file in wheel: {name}")
        with tempfile.TemporaryDirectory(prefix="vwb-wheel-smoke-") as temp:
            base = Path(temp)
            extracted, project = base / "wheel", base / "project"
            archive.extractall(extracted)
            project.mkdir()
            (project / ".env").write_text("# Empty smoke-test environment.\n")
            env = {key: value for key, value in os.environ.items()
                   if not key.endswith(("_API_KEY", "_TOKEN")) and key not in {"VWB_RUNS_DIR", "VWB_ENV_FILE", "PYTHONPATH"}}
            env.update(PYTHONPATH=str(extracted), VWB_ENV_FILE=str(project / ".env"), VWB_SMOKE_WHEEL_ROOT=str(extracted))
            if args.frontend_test:
                env["VWB_SMOKE_FRONTEND_TEST"] = str(args.frontend_test.resolve())
            subprocess.run([sys.executable, "-c", PROBE], cwd=project, env=env, check=True, timeout=90)
    print(f"Wheel smoke passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
