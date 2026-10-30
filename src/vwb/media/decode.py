"""Frame-exact CFR decoding through the system ffmpeg.

Two traps shape this module (spec R4): input-side seeking is not frame-exact unless the
seek time sits between frames, and swscale converts YUV to RGB with BT.601 by default.
We seek half a frame before the target, so the first frame out is exactly the target,
relative to the video stream's first timestamp, and name BT.709 explicitly. Negative
timestamps use ordinal selection from the beginning because demuxer seeks may clamp them.
One ffmpeg process serves one contiguous read.
Timing and geometry share the probe's lightweight, file-identity-aware metadata cache.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from numbers import Integral
from pathlib import Path
from threading import Thread

import numpy as np

from vwb.media.probe import _assert_identity, _metadata
from vwb.util.procs import ToolError, which


def _integer(value: int, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return int(value)


class FrameReader:
    """RGB frames by index for one constant-frame-rate video.

    Requested ranges must be valid; they are never silently clamped. Exhaust the
    iterator to verify the whole read. A changed input requires a fresh reader.
    """

    def __init__(self, path: Path | str, size: tuple[int, int] | None = None):
        metadata = _metadata(path)
        info = metadata.info
        self.path, self._identity = metadata.path, metadata.identity
        self.src_w, self.src_h, self.fps, self.n_frames = info.width, info.height, info.fps, info.n_frames
        self.start_time = info.start_time
        if size is not None and len(size) != 2:
            raise ValueError("Decode size must contain width and height")
        self.size = ((_integer(size[0], "width", 1), _integer(size[1], "height", 1))
                     if size is not None else (self.src_w, self.src_h))

    def __enter__(self) -> FrameReader:
        return self

    def __exit__(self, *exc) -> None:
        pass

    def _cmd(self, start: int, count: int, step: int) -> list[str]:
        w, h = self.size
        filters = []
        if self.start_time < 0:
            # Some demuxers clamp negative seeks to zero, dropping valid frames.
            # Select by decoded ordinal instead; this costs a prefix decode.
            filters.append(f"select='gte(n\\,{start})*not(mod(n-{start}\\,{step}))'")
            seek_args = []
        else:
            seek = float(self.start_time) + max(0.0, (start - 0.5) / float(self.fps))
            # Absolute timestamps avoid confusing video-relative frame indices
            # with a container clock that may begin at an earlier audio track.
            seek_args = ["-seek_timestamp", "1", "-ss", f"{seek:.6f}"]
        if step > 1 and self.start_time >= 0:
            filters.append(f"select='not(mod(n\\,{step}))'")
        flags = "area" if (w, h) != (self.src_w, self.src_h) else "bicubic"
        filters.append(f"scale={w}:{h}:flags={flags}+accurate_rnd+full_chroma_int:"
                       "in_color_matrix=bt709:in_range=tv:out_range=pc")
        filters.append("format=rgb24")
        return [which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", *seek_args, "-i", self.path,
                "-map", "0:v:0", "-an", "-sn", "-vf", ",".join(filters),
                "-fps_mode", "passthrough", "-frames:v", str(count),
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    def frames(self, start: int, end: int, step: int = 1) -> Iterator[tuple[int, np.ndarray]]:
        """Yield every `step`-th frame in [start, end), or raise on an incomplete decode."""
        start, end = _integer(start, "start"), _integer(end, "end")
        step = _integer(step, "step", 1)
        if end < start:
            raise ValueError(f"Frame range end {end} is before start {start}")
        if start > self.n_frames or end > self.n_frames:
            raise IndexError(f"Frame range [{start}, {end}) outside [0, {self.n_frames}) for {self.path}")
        _assert_identity(self.path, self._identity)
        if end == start:
            return
        count = (end - start + step - 1) // step
        w, h = self.size
        frame_bytes = w * h * 3
        proc = subprocess.Popen(self._cmd(start, count, step), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=frame_bytes * 2)
        assert proc.stdout is not None and proc.stderr is not None
        # Drain stderr concurrently, retaining only a bounded diagnostic tail.
        # An ffmpeg failure can fill a PIPE while stdout is still being read.
        error_tail = bytearray()

        def drain_errors():
            while chunk := proc.stderr.read(4096):
                error_tail.extend(chunk)
                del error_tail[:-2000]

        errors = Thread(target=drain_errors, name="vwb-ffmpeg-stderr", daemon=True)
        errors.start()
        got, partial = 0, 0
        try:
            while got < count:
                # Buffered reads normally fill the request, but short pipe reads
                # are legal. Only EOF proves an incomplete raw frame.
                buf = bytearray()
                while len(buf) < frame_bytes:
                    chunk = proc.stdout.read(frame_bytes - len(buf))
                    if not chunk:
                        break
                    buf.extend(chunk)
                if len(buf) != frame_bytes:
                    partial = len(buf)
                    break
                idx = start + got * step
                got += 1
                yield idx, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            try:
                code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise ToolError(f"ffmpeg did not exit after decoding {got}/{count} frames from {self.path}") from exc
            errors.join()
            detail = error_tail.decode(errors="replace").strip()
            if got != count:
                missing = start + got * step
                raise ToolError(f"Incomplete ffmpeg decode from {self.path}: requested {count} frames "
                                f"in [{start}, {end}) step {step}, decoded {got}; missing frame {missing} "
                                f"({partial}/{frame_bytes} bytes), exit {code}. {detail}")
            if code:
                raise ToolError(f"ffmpeg decode failed for {self.path} at frame {start} (exit {code}): {detail}")
            _assert_identity(self.path, self._identity)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()
            errors.join()
            proc.stderr.close()

    def frame(self, index: int) -> np.ndarray:
        index = _integer(index, "frame index")
        # Exhaust the one-frame iterator so exit-status/integrity checks run.
        return list(self.frames(index, index + 1))[0][1]


def read_frames(path: Path | str, indices: list[int], size: tuple[int, int] | None = None) -> dict[int, np.ndarray]:
    """Read every requested frame, or raise; never return a partial dictionary.

    Duplicate indices are deduplicated. Close indices share one decode.
    """
    wanted = sorted({_integer(i, "frame index") for i in indices})
    if not wanted:
        return {}
    reader = FrameReader(path, size=size)
    if wanted[-1] >= reader.n_frames:
        raise IndexError(f"Frame {wanted[-1]} outside [0, {reader.n_frames}) for {reader.path}")
    out: dict[int, np.ndarray] = {}
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
    missing = set(wanted).difference(out)
    if missing:
        raise ToolError(f"Missing requested frames from {reader.path}: {sorted(missing)}")
    return out


def sample_frames(path: Path | str, step: int, size: tuple[int, int] | None = None,
                  start: int = 0, end: int | None = None) -> Iterator[tuple[int, np.ndarray]]:
    """Every `step`-th frame in one decode pass."""
    reader = FrameReader(path, size=size)
    yield from reader.frames(start, end if end is not None else reader.n_frames, step)
