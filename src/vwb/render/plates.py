"""Content-addressed alpha plates dispatched through trusted renderer adapters."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock
from PIL import Image

from vwb.canvas.renderers import get_renderer, props_renderer
from vwb.config import GENERATORS_DIR
from vwb.media.probe import probe
from vwb.util.hashing import sha256_json
from vwb.util.procs import ToolError, which

REMOTION_DIR = GENERATORS_DIR / "remotion"  # shared pinned Node runtime, including non-Remotion adapters
GENERATOR_PIN = get_renderer("remotion").pin


def plate_id(composition: str, props: dict[str, Any]) -> str:
    if composition != "Scene":
        raise ValueError("Only the trusted Scene composition is supported")
    return sha256_json({"pin": props_renderer(props).pin, "props": props})


def ensure_installed(renderer: str = "remotion") -> None:
    get_renderer(renderer)
    dependencies = ("remotion",) if renderer == "remotion" else ("playwright-core", "esbuild", "react", "react-dom", "gsap")
    missing = [name for name in dependencies if not (REMOTION_DIR / "node_modules" / name).exists()]
    if missing:
        raise ToolError(f"{renderer} renderer dependencies missing ({', '.join(missing)}). "
                        f"Run `npm ci && npm run setup-fonts` in {REMOTION_DIR}, then install its Chromium browser.")


def _validate_props(props: dict) -> None:
    for key, upper in (("width", 8192), ("height", 8192), ("fps", 120), ("durationInFrames", 108000)):
        value = props.get(key)
        if type(value) is not int or not 1 <= value <= upper:
            raise ValueError(f"Plate {key} must be an integer in 1..{upper}")
    if props["width"] * props["height"] > 33554432:
        raise ValueError("Plate dimensions exceed 32 megapixels")
    if len(json.dumps(props)) > 16 * 1024 * 1024:
        raise ValueError("Plate input exceeds 16 MiB")
    props_renderer(props)


def _execute(folder: Path, jobs: list[dict], timeout: int) -> None:
    # Group by backend. Cached outputs and empty batches never require dependencies.
    for renderer in dict.fromkeys(job["renderer"] for job in jobs):
        selected = [job for job in jobs if job["renderer"] == renderer]
        ensure_installed(renderer)
        batch = folder / f"jobs_{renderer}.json"
        batch.write_text(json.dumps(selected))
        script = "render.mjs" if renderer == "remotion" else "browser-render.mjs"
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        # An explicit host-configured browser path is not supplied by canvas data.
        if os.environ.get("VWB_CHROMIUM_EXECUTABLE"):
            env["VWB_CHROMIUM_EXECUTABLE"] = os.environ["VWB_CHROMIUM_EXECUTABLE"]
        try:
            proc = subprocess.run([which("node"), str(REMOTION_DIR / script), str(batch)], cwd=REMOTION_DIR,
                                  capture_output=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"{renderer} render exceeded {timeout}s") from exc
        if proc.returncode:
            raise ToolError(f"{renderer} render failed: {proc.stderr.decode(errors='replace')[-2000:]}")


def render(plates_dir: Path, jobs: list[tuple[str, dict[str, Any]]]) -> list[str]:
    if not jobs:
        return []
    for composition, props in jobs:
        _validate_props(props)
        plate_id(composition, props)
    plates_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(plates_dir / ".render.lock")):
        return _render_locked(plates_dir, jobs)


def _render_locked(plates_dir: Path, jobs: list[tuple[str, dict[str, Any]]]) -> list[str]:
    refs = [f"plate:{plate_id(composition, props)}" for composition, props in jobs]
    with tempfile.TemporaryDirectory(prefix=".plates-", dir=plates_dir) as scratch:
        stage = Path(scratch)
        todo = {}
        for composition, props in jobs:
            pid = plate_id(composition, props)
            out = plates_dir / f"{pid}.mov"
            if not out.is_file() or not out.stat().st_size:
                todo[pid] = {"composition": composition, "props": props, "renderer": props_renderer(props).id,
                             "out": str(stage / f"{pid}.mov")}
        _execute(stage, list(todo.values()), 4 * 3600)
        for job in todo.values():
            output, props = Path(job["out"]), job["props"]
            if not output.is_file() or not output.stat().st_size:
                raise ToolError(f"Renderer reported success but wrote no plate: {output.name}")
            info = probe(output, with_keyframes=False)
            if (info.n_frames, info.width, info.height) != (props["durationInFrames"], props["width"], props["height"]):
                raise ToolError("Renderer returned the wrong frame count or dimensions")
            if "a" not in info.pix_fmt or float(info.fps) != props["fps"]:
                raise ToolError("Renderer returned a plate without alpha or at the wrong FPS")
        # Publish only after every result passes. Interrupted/failed renders never poison the cache.
        for pid, job in todo.items():
            Path(job["out"]).replace(plates_dir / f"{pid}.mov")
            (plates_dir / f"{pid}.props.json").write_text(json.dumps(job["props"], indent=1))
    return refs


def render_stills(plates_dir: Path, jobs: list[tuple[str, dict[str, Any], list[int]]]) -> dict[str, dict[int, Path]]:
    if not jobs:
        return {}
    for composition, props, frames in jobs:
        _validate_props(props)
        plate_id(composition, props)
        if any(type(frame) is not int or not 0 <= frame < props["durationInFrames"] for frame in frames):
            raise ValueError("Plate still frames must be integers within the local timeline")
    stills_dir = plates_dir / "stills"
    stills_dir.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict[int, Path]] = {}
    with FileLock(str(plates_dir / ".stills.lock")), tempfile.TemporaryDirectory(prefix=".stills-", dir=plates_dir) as scratch:
        stage = Path(scratch)
        todo = {}
        for composition, props, frames in jobs:
            pid = plate_id(composition, props)
            paths = found.setdefault(pid, {})
            paths.update({frame: stills_dir / f"{pid}_f{frame}.png" for frame in frames})
            missing = sorted(frame for frame, path in paths.items() if not path.is_file() or not path.stat().st_size)
            if missing:
                todo[pid] = {"composition": composition, "props": props, "renderer": props_renderer(props).id,
                             "out": str(stage / f"{pid}.mov"), "stills": missing, "stills_dir": str(stage), "pid": pid}
        _execute(stage, list(todo.values()), 1800)
        for pid, job in todo.items():
            for frame in job["stills"]:
                output = stage / f"{pid}_f{frame}.png"
                try:
                    with Image.open(output) as im:
                        im.load()
                        if im.size != (job["props"]["width"], job["props"]["height"]) or im.mode != "RGBA":
                            raise ToolError("Renderer returned a still with incorrect size or missing alpha")
                except (OSError, ValueError) as exc:
                    raise ToolError(f"Renderer wrote no valid still: {output.name}") from exc
        for pid, job in todo.items():
            for frame in job["stills"]:
                (stage / f"{pid}_f{frame}.png").replace(found[pid][frame])
    return found
