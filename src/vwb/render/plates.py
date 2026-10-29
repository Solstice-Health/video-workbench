"""Content-addressed alpha plates dispatched through trusted renderer adapters."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
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
        proc = subprocess.Popen([which("node"), str(REMOTION_DIR / script), str(batch)], cwd=REMOTION_DIR,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                start_new_session=os.name == "posix")
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_renderer(proc)
            raise ToolError(f"{renderer} render exceeded {timeout}s") from exc
        except BaseException:
            _stop_renderer(proc)
            raise
        if proc.returncode:
            raise ToolError(f"{renderer} render failed: {stderr.decode(errors='replace')[-2000:]}")


def _stop_renderer(proc: subprocess.Popen) -> None:
    """Allow graceful cleanup, then stop identity-checked descendants, including setsid children."""
    def snapshot(pid=None, timeout=1):
        # No shell or inherited PATH: ps only reads process ancestry and start times.
        if pid is not None and (type(pid) is not int or pid <= 1):
            return {}
        command = ["ps", "-axo", "pid=,ppid=,lstart="] if pid is None else [
            "ps", "-p", str(pid), "-o", "pid=,ppid=,lstart="]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                    env={"PATH": os.defpath, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if result.returncode:
            return {}
        rows = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 7 or not all(field.isascii() and field.isdecimal() for field in fields[:2]):
                continue
            child, parent = map(int, fields[:2])
            if child > 1 and child != parent:
                rows[child] = (parent, " ".join(fields[2:]))
        return rows

    # Capture ancestry while Node is still our unreaped child. After TERM, a
    # detached Chromium may be reparented and can no longer be discovered safely.
    owned = []
    if os.name == "posix" and proc.poll() is None:
        before = snapshot()
        children = {}
        for pid, (parent, _) in before.items():
            children.setdefault(parent, []).append(pid)
        pending, seen = [(proc.pid, 0)], {proc.pid}
        while pending:
            parent, depth = pending.pop()
            for pid in children.get(parent, []):
                if pid not in seen:
                    seen.add(pid)
                    owned.append((depth + 1, pid, before[pid][1]))
                    pending.append((pid, depth + 1))

    def stop(force=False):
        try:
            # Popen still owns this unreaped child; an exited/reaped PID must
            # never be used to signal a possibly recycled process group.
            if proc.poll() is not None:
                return
            if os.name == "posix" and os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
            else:
                proc.kill() if force else proc.terminate()
        except OSError:
            pass

    # Browser adapters allow 5s for close and 5s for targeted forced cleanup.
    # Leave time for both before the parent takes over descendant cleanup.
    grace_deadline = time.monotonic() + 12
    stop()
    try:
        proc.communicate(timeout=max(.01, grace_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass
    else:
        if owned:
            current = snapshot()
            if any(current.get(pid, (None, None))[1] == identity for _, pid, identity in owned):
                # Node can exit and close its pipes before its detached children.
                time.sleep(max(0, grace_deadline - time.monotonic()))

    cleanup_deadline = time.monotonic() + 5
    # Deepest children first, allowing surviving parents to reap them. A fresh
    # start-time check fences PID reuse; PPID is allowed to change after TERM.
    for _, pid, identity in sorted(owned, reverse=True):
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            break
        if snapshot(pid, min(1, remaining)).get(pid, (None, None))[1] == identity:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    stop(force=True)
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        # A descendant we could not safely identify may still own a pipe. Do
        # not hang forever, or broaden cleanup to processes we do not own.
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


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
            if not info.pix_fmt.startswith("yuva444p") or info.vcodec != "prores" or float(info.fps) != props["fps"]:
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
                        if im.size != (job["props"]["width"], job["props"]["height"]) or im.mode not in {"RGB", "RGBA"}:
                            raise ToolError("Renderer returned a still with incorrect size or missing alpha")
                        # Chromium omits the alpha channel when the entire frame is opaque.
                        # Preserve those pixels and normalize the cache to the RGBA contract.
                        if im.mode == "RGB":
                            im.convert("RGBA").save(output)
                except (OSError, ValueError) as exc:
                    raise ToolError(f"Renderer wrote no valid still: {output.name}") from exc
        for pid, job in todo.items():
            for frame in job["stills"]:
                (stage / f"{pid}_f{frame}.png").replace(found[pid][frame])
    return found
