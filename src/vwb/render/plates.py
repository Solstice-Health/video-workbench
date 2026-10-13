"""Generator plates: render alpha overlays with Remotion, cached by content hash.

The plate id hashes the generator pin, composition and props, so the same props never
render twice, and a canvas cites its plate as plate:<sha256> (spec D37: generators render
hashed alpha assets; the canvas holds no code).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from vwb.config import GENERATORS_DIR
from vwb.util.hashing import sha256_json
from vwb.util.log import log
from vwb.util.procs import ToolError, which

REMOTION_DIR = GENERATORS_DIR / "remotion"
GENERATOR_PIN = "remotion@4.0.534:Scene@2"


def plate_id(composition: str, props: dict[str, Any]) -> str:
    from vwb.canvas.compile import PLATE_PIN
    return sha256_json({"pin": PLATE_PIN, "props": props})


def ensure_installed() -> None:
    if not (REMOTION_DIR / "node_modules" / "remotion").exists():
        raise ToolError("Remotion generator is not installed: run `npm install && npm run setup-fonts` "
                        f"in {REMOTION_DIR}")


def render(plates_dir: Path, jobs: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Render missing plates. Returns refs (plate:<sha>) in job order."""
    ensure_installed()
    plates_dir.mkdir(parents=True, exist_ok=True)
    from filelock import FileLock
    with FileLock(str(plates_dir / ".render.lock")):   # one Remotion render at a time; each uses most cores
        return _render_locked(plates_dir, jobs)


def _render_locked(plates_dir: Path, jobs: list[tuple[str, dict[str, Any]]]) -> list[str]:
    refs, todo = [], []
    for composition, props in jobs:
        pid = plate_id(composition, props)
        refs.append(f"plate:{pid}")
        out = plates_dir / f"{pid}.mov"
        if not out.exists():
            todo.append({"composition": composition, "props": props, "out": str(out)})
            (plates_dir / f"{pid}.props.json").write_text(json.dumps(props, indent=1))
    if todo:
        batch = plates_dir / f"jobs_{sha256_json(todo)[:12]}.json"
        batch.write_text(json.dumps(todo))
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}  # no vendor keys
        log.info("rendering %d overlay plate(s) with Remotion", len(todo))
        proc = subprocess.run([which("node"), str(REMOTION_DIR / "render.mjs"), str(batch)], cwd=REMOTION_DIR,
                              capture_output=True, env=env, timeout=4 * 3600)
        if proc.returncode != 0:
            raise ToolError(f"Remotion render failed: {proc.stderr.decode(errors='replace')[-2000:]}")
        for job in todo:
            if not Path(job["out"]).exists():
                raise ToolError(f"Remotion reported success but wrote no plate: {job['out']}")
    return refs


def render_stills(plates_dir: Path, jobs: list[tuple[str, dict[str, Any], list[int]]]) -> dict[str, dict[int, Path]]:
    """Single frames of plates (local frame numbers) as PNGs with alpha, for previews: a preview costs
    a few stills instead of whole plates. Cached by plate id and frame; a lock of their own, so a
    preview does not wait behind a batch render."""
    ensure_installed()
    stills_dir = plates_dir / "stills"
    stills_dir.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict[int, Path]] = {}
    todo = []
    for composition, props, frames in jobs:
        pid = plate_id(composition, props)
        paths = {int(fr): stills_dir / f"{pid}_f{int(fr)}.png" for fr in frames}
        found[pid] = paths
        missing = sorted(fr for fr, path in paths.items() if not path.exists())
        if missing:
            todo.append({"composition": composition, "props": props, "out": str(stills_dir / f"{pid}.mov"),
                         "stills": missing, "stills_dir": str(stills_dir), "pid": pid})
    if todo:
        from filelock import FileLock
        with FileLock(str(plates_dir / ".stills.lock")):
            batch = stills_dir / f"jobs_{sha256_json(todo)[:12]}.json"
            batch.write_text(json.dumps(todo))
            env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
            proc = subprocess.run([which("node"), str(REMOTION_DIR / "render.mjs"), str(batch)], cwd=REMOTION_DIR,
                                  capture_output=True, env=env, timeout=1800)
            if proc.returncode != 0:
                raise ToolError(f"Remotion still render failed: {proc.stderr.decode(errors='replace')[-2000:]}")
    return found
