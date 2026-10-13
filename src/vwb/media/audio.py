"""Audio analysis: PCM, RMS envelope, silences and EBU R128 loudness."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from vwb.util.procs import run, which


def load_pcm(path: Path | str, sr: int = 16000, mono: bool = True,
             start: float | None = None, end: float | None = None) -> np.ndarray:
    cmd = [which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.4f}"]
    cmd += ["-i", str(path)]
    if end is not None:
        cmd += ["-t", f"{max(0.0, end - (start or 0.0)):.4f}"]
    cmd += ["-vn", "-ac", "1" if mono else "2", "-ar", str(sr), "-f", "f32le", "-"]
    proc = run(cmd)
    pcm = np.frombuffer(proc.stdout, dtype=np.float32)
    return pcm if mono else pcm.reshape(-1, 2)


def rms_envelope(pcm: np.ndarray, sr: int, hop_ms: float = 10.0, win_ms: float = 25.0) -> tuple[np.ndarray, np.ndarray]:
    """Return (times, dBFS) sampled every hop_ms."""
    hop = max(1, int(sr * hop_ms / 1000))
    win = max(hop, int(sr * win_ms / 1000))
    if len(pcm) < win:
        return np.zeros(0), np.zeros(0)
    sq = np.concatenate([[0.0], np.cumsum(pcm.astype(np.float64) ** 2)])
    starts = np.arange(0, len(pcm) - win, hop)
    energy = (sq[starts + win] - sq[starts]) / win
    db = 10 * np.log10(np.maximum(energy, 1e-12))
    times = (starts + win / 2) / sr
    return times, db


_SIL_START = re.compile(r"silence_start: (-?[\d.]+)")
_SIL_END = re.compile(r"silence_end: (-?[\d.]+) \| silence_duration: ([\d.]+)")


def silences(path: Path | str, noise_db: float = -40.0, min_dur: float = 0.2) -> list[tuple[float, float]]:
    proc = run([which("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path), "-vn",
                "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    text = proc.stderr.decode(errors="replace")
    out, start = [], None
    for line in text.splitlines():
        m = _SIL_START.search(line)
        if m:
            start = float(m.group(1))
            continue
        m = _SIL_END.search(line)
        if m and start is not None:
            out.append((max(0.0, start), float(m.group(1))))
            start = None
    return out


_I = re.compile(r"I:\s+(-?[\d.]+) LUFS")
_LRA = re.compile(r"LRA:\s+(-?[\d.]+) LU")
_TP = re.compile(r"Peak:\s+(-?[\d.]+) dBFS")


def loudness(path: Path | str) -> dict[str, float | None]:
    """Integrated loudness (LUFS), loudness range (LU) and true peak (dBTP) via ebur128."""
    proc = run([which("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path), "-vn",
                "-af", "ebur128=peak=true", "-f", "null", "-"])
    text = proc.stderr.decode(errors="replace")
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else text
    def grab(rx):
        m = rx.search(summary)
        return float(m.group(1)) if m else None
    return {"integrated_lufs": grab(_I), "lra_lu": grab(_LRA), "true_peak_dbtp": grab(_TP)}


def channel_balance_db(path: Path | str) -> float | None:
    """Absolute RMS difference between left and right channels, in dB."""
    try:
        pcm = load_pcm(path, sr=16000, mono=False)
    except Exception:
        return None
    if pcm.ndim != 2 or len(pcm) == 0:
        return None
    rms = np.sqrt(np.mean(pcm.astype(np.float64) ** 2, axis=0) + 1e-12)
    return float(abs(20 * np.log10(rms[0] / rms[1])))
