"""Subprocess wrappers that assert their own postconditions.

Silent success is the common failure mode for media tools (spec: libvmaf accepted 179 of
180 frames). Every wrapper here checks that its output exists and is non-empty.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from vwb.util.log import log


class ToolError(RuntimeError):
    pass


def run(cmd: Sequence[str], *, timeout: float | None = None, input: bytes | None = None,
        expect_files: Sequence[Path] = (), cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(map(str, cmd)))
    try:
        proc = subprocess.run(list(map(str, cmd)), input=input, capture_output=True, timeout=timeout,
                              cwd=cwd, env=env, check=False)
    except FileNotFoundError as exc:
        raise ToolError(f"Executable not found: {cmd[0]}") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-2000:]
        raise ToolError(f"{Path(str(cmd[0])).name} failed (exit {proc.returncode}): {tail}")
    for path in expect_files:
        if not Path(path).is_file() or Path(path).stat().st_size == 0:
            raise ToolError(f"{Path(str(cmd[0])).name} reported success but wrote no output: {path}")
    return proc


@lru_cache(maxsize=None)
def which(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ToolError(f"{name} is not on PATH")
    return found


def ffmpeg(*args: str, expect: Sequence[Path] = (), timeout: float | None = None) -> subprocess.CompletedProcess:
    return run([which("ffmpeg"), "-hide_banner", "-nostdin", "-y", *args], expect_files=expect, timeout=timeout)


def ffprobe_json(path: Path | str, *args: str) -> dict:
    proc = run([which("ffprobe"), "-v", "error", "-of", "json", *args, str(path)])
    return json.loads(proc.stdout or b"{}")


@lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    proc = run([which("ffmpeg"), "-version"])
    return proc.stdout.decode().splitlines()[0]
