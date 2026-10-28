"""Person mattes for source footage (RobustVideoMatting, open weights, run locally with ONNX Runtime).

A `video` layer with `matte: "person"` shows only the people in the source: the speaker can stand
on a designed page instead of sitting in a boxed crop. The matte is computed once per source span
(the recurrent model runs over the frames in order, with a short warm-up), cached as half-resolution
alpha in a memory map, and looked up by source frame, so it stays in sync with the time map.
Provenance: segmented source pixels; the verifier raises MATTE-RISK for every output that uses one.
"""

from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from vwb.media.decode import FrameReader

MODEL_URL = "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx"
MODEL = Path.home() / ".cache" / "vwb" / "models" / "rvm_mobilenetv3_fp32.onnx"
VERSION = "rvm-mbv3-3"  # source-specific face guidance; invalidate masks made with another source's index
_lock = threading.Lock()
_session = None


def session():
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort
            if not MODEL.exists():
                MODEL.parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(MODEL_URL, MODEL)       # 15 MB, once
            prov = [p for p in ("CoreMLExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
            _session = ort.InferenceSession(str(MODEL), providers=prov)
        return _session


def cache_path(cache_dir: Path, media_sha: str, f0: int, f1: int) -> Path:
    return cache_dir / f"{media_sha[:16]}_{f0}_{f1}_{VERSION}.npy"


def _person_only(a: np.ndarray, faces: list[tuple[float, float, float, float]], scale: float) -> np.ndarray:
    """Keep the matte regions that belong to a person: connected to a body region grown from a face box
    (the model also keeps salient graphics such as logos as foreground). Without faces, keep the largest region."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats((a > 64).astype(np.uint8), connectivity=8)
    if n <= 1:
        return a
    h, w = a.shape
    keep = np.zeros(n, bool)
    if faces:
        for fx, fy, fw, fh in faces:
            x0, x1 = int((fx - 1.6 * fw) * scale), int((fx + 2.6 * fw) * scale)
            y0, y1 = int((fy - 0.4 * fh) * scale), h
            region = lab[max(0, y0):y1, max(0, x0):min(w, x1)]
            keep[np.unique(region[region > 0])] = True
    if not keep.any():
        keep[1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))] = True
    keep[0] = False
    out = np.where(keep[lab], a, 0).astype(np.uint8)
    return cv2.GaussianBlur(out, (3, 3), 0) if out.any() else out


def matte_span(media: Path, media_sha: str, f0: int, f1: int, cache_dir: Path, warmup: int = 12,
               faces: dict[int, list[tuple[float, float, float, float]]] | None = None) -> np.ndarray:
    """Alpha (uint8, half resolution) for source frames [f0, f1), cached; returns a read-only memory map.
    faces (source frame -> face boxes, source pixels) restricts the matte to people."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(cache_dir, media_sha, f0, f1)
    if path.exists():
        return np.load(path, mmap_mode="r")
    sess = session()
    rec = [np.zeros((1, 1, 1, 1), np.float32)] * 4
    ratio = None
    out = None
    tmp = path.with_suffix(".part.npy")
    start = max(0, f0 - warmup)
    with _lock:                                                      # one matte job at a time: it uses the ANE fully
        for f, rgb in FrameReader(media).frames(start, f1):
            h, w = rgb.shape[:2]
            if ratio is None:
                ratio = np.array([min(1.0, 512 / max(h, w)) * 1.0], np.float32)
                out = np.lib.format.open_memmap(tmp, mode="w+", dtype=np.uint8, shape=(f1 - f0, h // 2, w // 2))
            x = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
            _, pha, *rec = sess.run(None, {"src": x, "r1i": rec[0], "r2i": rec[1], "r3i": rec[2], "r4i": rec[3],
                                           "downsample_ratio": ratio})
            if f >= f0:
                a = cv2.resize((np.clip(pha[0, 0], 0, 1) * 255).astype(np.uint8), (w // 2, h // 2), interpolation=cv2.INTER_AREA)
                if faces is not None:
                    near = [b for d in (0, -1, 1, -2, 2, -3, 3) for b in faces.get(f + d, [])]
                    a = _person_only(a, near, 0.5)
                out[f - f0] = a
    assert out is not None
    out.flush()
    del out
    tmp.rename(path)
    return np.load(path, mmap_mode="r")


def faces_by_frame(store, f0: int, f1: int) -> dict[int, list[tuple[float, float, float, float]]]:
    """Face boxes per source frame from the edit index (sampled frames; neighbours fill gaps)."""
    out: dict[int, list[tuple[float, float, float, float]]] = {}
    for r in store.all("SELECT f, x, y, w, h FROM faces WHERE f >= ? AND f < ?", (f0 - 30, f1 + 30)):
        out.setdefault(int(r["f"]), []).append((r["x"], r["y"], r["w"], r["h"]))
    if not out:
        return {}
    keys = sorted(out)
    filled = {}
    for f in range(f0, f1):                                    # the nearest sampled frame's faces
        k = min(keys, key=lambda x: abs(x - f))
        if abs(k - f) <= 30:
            filled[f] = out[k]
    return filled


def full_alpha(half: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return cv2.resize(np.asarray(half), size, interpolation=cv2.INTER_LINEAR)


def stats(maps: list[np.ndarray]) -> dict[str, float]:
    """Coverage and frame-to-frame flicker of a matte (a jittery matte reads as boiling edges)."""
    cov, flick = [], []
    for m in maps:
        a = np.asarray(m[:: max(1, len(m) // 60)]).astype(np.float32) / 255.0
        cov.append(float((a > 0.5).mean()))
        if len(a) > 1:
            flick.append(float(np.abs(np.diff(a, axis=0)).mean()))
    return {"coverage": round(float(np.mean(cov)), 4) if cov else 0.0,
            "flicker": round(float(np.mean(flick)), 5) if flick else 0.0}
