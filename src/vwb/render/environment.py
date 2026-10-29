"""Pin actual browser-render dependencies without launching a browser or installing anything."""
from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

from vwb.config import GENERATORS_DIR
from vwb.util.hashing import sha256_json
from vwb.util.procs import ToolError, which

if TYPE_CHECKING:
    from vwb.canvas.model import Canvas


def capture() -> dict:
    from vwb.render.plates import ensure_installed
    ensure_installed("browser")
    runtime = GENERATORS_DIR / "remotion"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    if os.environ.get("VWB_CHROMIUM_EXECUTABLE"):
        env["VWB_CHROMIUM_EXECUTABLE"] = os.environ["VWB_CHROMIUM_EXECUTABLE"]
    try:
        result = subprocess.run([which("node"), str(runtime / "browser-render.mjs"), "--environment"],
                                cwd=runtime, capture_output=True, timeout=60, env=env)
    except subprocess.TimeoutExpired as exc:
        raise ToolError("Renderer environment inspection timed out") from exc
    if result.returncode:
        raise ToolError(f"Renderer environment unavailable: {result.stderr.decode(errors='replace')[-1500:]}")
    try:
        data = json.loads(result.stdout)
        if not isinstance(data.get("details"), dict) or data.get("sha256") != sha256_json(data["details"]):
            raise ValueError("invalid environment digest")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ToolError("Renderer returned invalid environment metadata") from exc
    return data


def needed(canvas: Canvas) -> bool:
    return canvas.renderer != "remotion" and any(layer.type in {"text", "captions", "shape"} for layer in canvas.layers)


def bind(canvas: Canvas) -> None:
    """Called for working previews and new commits; never mutate an older version on disk."""
    canvas.renderer_environment = capture() if needed(canvas) else None


def validate(canvas: Canvas, current: dict | None = None) -> None:
    if not needed(canvas):
        return
    if not canvas.renderer_environment:
        raise ToolError("This canvas has no pinned renderer environment. Preview and commit a new version first.")
    current = current or capture()
    if canvas.renderer_environment.get("sha256") != current["sha256"]:
        raise ToolError("The browser renderer environment changed (dependencies, fonts, Chromium, encoder or OS). "
                        "Use the pinned environment, or preview and commit a new version. Existing renders remain available.")
