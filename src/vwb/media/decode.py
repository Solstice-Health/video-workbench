"""Frame-exact decoding through the system ffmpeg.

Two traps shape this module (spec R4): input-side seeking is not frame-exact unless the
seek time sits between frames, and swscale converts YUV to RGB with BT.601 by default.
We seek half a frame before the target, so the first frame out is exactly the target,
and we name the BT.709 matrix explicitly. One ffmpeg process serves one contiguous read.
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import numpy as np

from vwb.util.procs import ToolError, ffprobe_json, which


@lru_cache(maxsize=64)
def _stream_info(path: str) -> tuple[int, int, Fraction, int]:
    data = ffprobe_json(path, "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate,nb_frames,duration")
    s = data["streams"][0]
    num, _, den = s["r_frame_rate"].partition("/")
    fps = Fraction(int(num), int(den or 1))
    n = int(s.get("nb_frames") or round(float(s.get("duration") or 0) * float(fps)))
    return int(s["width"]), int(s["height"]), fps, n


class FrameReader:
    """RGB frames by index for one constant-frame-rate video."""

    def __init__(self, path: Path | str, size: tuple[int, int] | None = None):
        self.path = str(path)
        self.src_w, self.src_h, self.fps, self.n_frames = _stream_info(self.path)
        self.size = size or (self.src_w, self.src_h)

    def __enter__(self) -> "FrameReader":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def _cmd(self, start: int, count: int, step: int) -> list[str]:
        w, h = self.size
        filters = []
        if step > 1:
            filters.append(f"select='not(mod(n\\,{step}))'")
        flags = "area" if (w, h) != (self.src_w, self.src_h) else "bicubic"
        filters.append(f"scale={w}:{h}:flags={flags}+accurate_rnd+full_chroma_int:"
                       "in_color_matrix=bt709:in_range=tv:out_range=pc")
        filters.append("format=rgb24")
        seek = max(0.0, (start - 0.5) / float(self.fps))
        return [which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{seek:.6f}", "-i", self.path,
                "-an", "-sn", "-vf", ",".join(filters), "-fps_mode", "passthrough", "-frames:v", str(count),
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    def frames(self, start: int, end: int, step: int = 1) -> Iterator[tuple[int, np.ndarray]]:
        """Yield (index, rgb) for start <= index < end, every `step` frames."""
        start = max(0, start)
        end = min(end, self.n_frames)
        if end <= start:
            return
        count = (end - start + step - 1) // step
        w, h = self.size
        frame_bytes = w * h * 3
        proc = subprocess.Popen(self._cmd(start, count, step), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=frame_bytes * 2)
        assert proc.stdout is not None
        got = 0
        try:
            while got < count:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                yield start + got * step, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                got += 1
        finally:
            proc.stdout.close()
            proc.kill()
            proc.wait()
        if got == 0:
            err = proc.stderr.read().decode(errors="replace")[-600:] if proc.stderr else ""
            raise ToolError(f"ffmpeg decoded no frames from {self.path} at {start}: {err}")

    def frame(self, index: int) -> np.ndarray:
        for _, rgb in self.frames(index, index + 1):
            return rgb
        raise IndexError(f"Frame {index} not found in {self.path}")


def read_frames(path: Path | str, indices: list[int], size: tuple[int, int] | None = None) -> dict[int, np.ndarray]:
    """Read a sparse, sorted set of frames. Close indices share one decode."""
    out: dict[int, np.ndarray] = {}
    wanted = sorted(set(int(i) for i in indices))
    if not wanted:
        return out
    reader = FrameReader(path, size=size)
    i = 0
    while i < len(wanted):
        j = i
        while j + 1 < len(wanted) and wanted[j + 1] - wanted[j] <= 45:
            j += 1
        group = set(wanted[i:j + 1])
        for idx, rgb in reader.frames(wanted[i], wanted[j] + 1):
            if idx in group:
                out[idx] = rgb.copy()
        i = j + 1
    return out


def sample_frames(path: Path | str, step: int, size: tuple[int, int] | None = None,
                  start: int = 0, end: int | None = None) -> Iterator[tuple[int, np.ndarray]]:
    """Every `step`-th frame in one decode pass."""
    reader = FrameReader(path, size=size)
    yield from reader.frames(start, end if end is not None else reader.n_frames, step)
