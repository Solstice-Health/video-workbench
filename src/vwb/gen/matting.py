"""Person mattes for source footage (RobustVideoMatting, open weights, run locally with ONNX Runtime).

A `video` layer with `matte: "person"` shows only the people in the source: the speaker can stand
on a designed page instead of sitting in a boxed crop. The matte is computed once per source span
(the recurrent model runs over the frames in order, with a short warm-up), cached as half-resolution
alpha in a memory map, and looked up by source frame, so it stays in sync with the time map.
Provenance: segmented source pixels; the verifier raises MATTE-RISK for every output that uses one.
"""

from __future__ import annotations

import math
import os
import tempfile
import threading
import urllib.request
from numbers import Integral
from pathlib import Path

import cv2
import numpy as np

from vwb.media.decode import FrameReader
from vwb.util.hashing import sha256_json

MODEL_URL = "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx"
MODEL = Path.home() / ".cache" / "vwb" / "models" / "rvm_mobilenetv3_fp32.onnx"
VERSION = "rvm-mbv3-4"  # guidance/warmup identity and complete, atomic cache publication
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


def _guidance(faces):
    """Copy and normalize guidance so the cache key describes the actual run.

    None disables component filtering; {} enables largest-person filtering.
    These must stay distinct even though both contain no face boxes.
    """
    if faces is None:
        return None
    if not isinstance(faces, dict):
        raise ValueError("faces must be a frame-to-boxes dictionary or None")
    normalized = {}
    for frame, boxes in faces.items():
        if isinstance(frame, bool) or not isinstance(frame, Integral) or frame < 0:
            raise ValueError("Face guidance frame indices must be non-negative integers")
        normalized[int(frame)] = []
        for box in boxes:
            if len(box) != 4 or any(isinstance(v, bool) for v in box):
                raise ValueError("Face guidance boxes must contain four finite coordinates")
            values = tuple(float(v) for v in box)
            if not all(math.isfinite(v) for v in values) or values[2] <= 0 or values[3] <= 0:
                raise ValueError("Face guidance boxes need finite coordinates and positive dimensions")
            normalized[int(frame)].append(values)
        normalized[int(frame)].sort()
    return normalized


def cache_path(cache_dir: Path, media_sha: str, f0: int, f1: int, warmup: int = 12,
               faces: dict[int, list[tuple[float, float, float, float]]] | None = None) -> Path:
    guidance = _guidance(faces)
    digest = sha256_json({"media_sha": media_sha, "span": [f0, f1], "warmup": warmup, "version": VERSION,
                          "faces": sorted(guidance.items()) if guidance is not None else None})
    return cache_dir / f"{media_sha[:16]}_{f0}_{f1}_{VERSION}_{digest[:32]}.npy"


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
            x0, x1 = max(0, min(w, x0)), max(0, min(w, x1))
            y0, y1 = max(0, min(h, y0)), max(0, min(h, y1))
            region = lab[y0:y1, x0:x1]
            keep[np.unique(region[region > 0])] = True
    if not keep.any():
        keep[1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))] = True
    keep[0] = False
    out = np.where(keep[lab], a, 0).astype(np.uint8)
    return cv2.GaussianBlur(out, (3, 3), 0) if out.any() else out


def matte_span(media: Path, media_sha: str, f0: int, f1: int, cache_dir: Path, warmup: int = 12,
               faces: dict[int, list[tuple[float, float, float, float]]] | None = None) -> np.ndarray:
    """Alpha (uint8, half resolution) for source frames [f0, f1), cached; returns a read-only memory map.
    faces (source frame -> face boxes, source pixels) restricts the matte to people.
    None keeps the model's full alpha; {} keeps its largest connected component.
    Only complete decodes/inference runs are published to the cache."""
    for name, value in (("f0", f0), ("f1", f1), ("warmup", warmup)):
        if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    f0, f1, warmup = int(f0), int(f1), int(warmup)
    if f1 <= f0:
        raise ValueError("Matte span must contain at least one frame (f1 > f0)")
    faces = _guidance(faces)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(cache_dir, media_sha, f0, f1, warmup=warmup, faces=faces)
    if path.exists():
        return np.load(path, mmap_mode="r", allow_pickle=False)
    sess = session()
    rec = [np.zeros((1, 1, 1, 1), np.float32)] * 4
    ratio = None
    out = None
    start = max(0, f0 - warmup)
    with _lock:                                                      # one matte job at a time: it uses the ANE fully
        if path.exists():
            return np.load(path, mmap_mode="r", allow_pickle=False)
        fd, name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".part.npy", dir=cache_dir)
        os.close(fd)
        tmp = Path(name)
        expected, shape = start, None
        try:
            for f, rgb in FrameReader(media).frames(start, f1):
                if f != expected or f >= f1:
                    raise ValueError(f"Incomplete matte decode: expected frame {expected}, received {f}")
                h, w = rgb.shape[:2]
                if shape is None:
                    if min(h, w) < 2:
                        raise ValueError("Matting requires video dimensions of at least 2 by 2 pixels")
                    shape = rgb.shape
                    ratio = np.array([min(1.0, 512 / max(h, w))], np.float32)
                    out = np.lib.format.open_memmap(tmp, mode="w+", dtype=np.uint8, shape=(f1 - f0, h // 2, w // 2))
                elif rgb.shape != shape:
                    raise ValueError(f"Video dimensions changed during matting at frame {f}")
                x = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
                _, pha, *rec = sess.run(None, {"src": x, "r1i": rec[0], "r2i": rec[1], "r3i": rec[2], "r4i": rec[3],
                                               "downsample_ratio": ratio})
                if f >= f0:
                    a = cv2.resize((np.clip(pha[0, 0], 0, 1) * 255).astype(np.uint8), (w // 2, h // 2), interpolation=cv2.INTER_AREA)
                    if faces is not None:
                        near = [b for d in (0, -1, 1, -2, 2, -3, 3) for b in faces.get(f + d, [])]
                        a = _person_only(a, near, 0.5)
                    out[f - f0] = a
                expected += 1
            if expected != f1 or out is None:
                raise ValueError(f"Incomplete matte decode: expected frames [{start}, {f1}), stopped at {expected}")
            out.flush()
            out._mmap.close()
            out = None
            tmp.replace(path)
        finally:
            if out is not None:
                out._mmap.close()
            tmp.unlink(missing_ok=True)
    return np.load(path, mmap_mode="r", allow_pickle=False)


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
