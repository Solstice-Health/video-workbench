"""Key colour plates in, alpha clips out (keyed motion).

The video model has no alpha channel. To animate an element over a design, the element is
placed on a flat key colour (the plate), the model is told to keep that background flat, and
the result is keyed back to alpha:
  choose_key   the candidate key colour farthest from every colour in the elements (validated
               before any clip is paid for: a key that occurs inside the subject punches holes)
  plate        an RGBA element composited on the key colour at a chosen place and size
  key_frame    a difference key against the frame's own measured background colour (the model
               drifts the green slightly), with colour unmixing and spill suppression
  key_clip     every frame of a clip, written as ProRes 4444 with alpha, plus matte metrics
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from vwb.util.procs import which

KEYS = {"green": (0, 177, 64), "blue": (0, 71, 187), "magenta": (255, 0, 255)}


def _chroma(rgb: np.ndarray) -> np.ndarray:
    """Cb, Cr (BT.709) of RGB values, float."""
    rgb = rgb.astype(np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return np.stack([(b - y) / 1.8556, (r - y) / 1.5748], axis=-1)


def choose_key(elements: list[np.ndarray], prefer: str | None = None) -> tuple[str, tuple[int, int, int], dict[str, float]]:
    """Pick the key colour whose chroma is farthest from the elements' opaque pixels.
    Returns its name, RGB, and per-candidate share of element pixels close to it (lower is better)."""
    px = [e[..., :3][e[..., 3] > 128] for e in elements if e.shape[2] == 4]
    px = [p for p in px if p.size]
    pix = np.concatenate(px) if px else np.zeros((1, 3), np.uint8)
    if len(pix) > 200000:
        pix = pix[np.random.default_rng(0).choice(len(pix), 200000, replace=False)]
    ch = _chroma(pix)
    risk = {}
    for name, rgb in KEYS.items():
        k = _chroma(np.array([rgb], np.uint8))[0]
        risk[name] = round(float((np.linalg.norm(ch - k, axis=1) < 40).mean()), 4)
    name = prefer if prefer in KEYS else min(risk, key=risk.get)
    return name, KEYS[name], risk


def plate(elements: list[tuple[np.ndarray, list[float]]], size: tuple[int, int], key_rgb: tuple[int, int, int]) -> np.ndarray:
    """Composite RGBA elements, each fitted into its box [x, y, w, h], onto a flat key colour."""
    W, H = size
    out = np.zeros((H, W, 3), np.float32)
    out[:] = key_rgb
    for rgba, box in elements:
        x, y, w, h = box
        k = min(w / rgba.shape[1], h / rgba.shape[0])
        nw, nh = max(1, round(rgba.shape[1] * k)), max(1, round(rgba.shape[0] * k))
        el = cv2.resize(rgba, (nw, nh), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC).astype(np.float32)
        ox, oy = int(round(x + (w - nw) / 2)), int(round(y + (h - nh) / 2))
        x0, y0, x1, y1 = max(0, ox), max(0, oy), min(W, ox + nw), min(H, oy + nh)
        if x1 <= x0 or y1 <= y0:
            continue
        sub = el[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
        a = sub[..., 3:4] / 255.0
        out[y0:y1, x0:x1] = sub[..., :3] * a + out[y0:y1, x0:x1] * (1 - a)
    return np.clip(out, 0, 255).astype(np.uint8)


KEY_NEAR = 45.0          # chroma distance within which a border pixel counts as the requested key colour


def background_key(rgb: np.ndarray, band: float = 0.03,
                   expected: tuple[int, int, int] | None = None) -> tuple[np.ndarray, float]:
    """The frame's key colour and the border's chroma noise. The video model drifts the requested colour, so the
    key is measured on the frame's border - but only on border pixels near the requested colour (`expected`) when
    it is known: a subject that touches or fills the border (a full-frame background element) must not be
    measured as the key. With too few such pixels, the requested colour itself is used."""
    h, w = rgb.shape[:2]
    bh, bw = max(2, int(h * band)), max(2, int(w * band))
    border = np.concatenate([rgb[:bh].reshape(-1, 3), rgb[-bh:].reshape(-1, 3),
                             rgb[:, :bw].reshape(-1, 3), rgb[:, -bw:].reshape(-1, 3)])
    if expected is not None:
        target = _chroma(np.array([expected], np.uint8))[0]
        near = border[np.linalg.norm(_chroma(border) - target, axis=1) < KEY_NEAR]
        if len(near) < max(50, 0.05 * len(border)):
            return np.array(expected, np.float32), 1.0
        border = near
    key = np.median(border, axis=0)
    noise = float(np.linalg.norm(_chroma(border) - _chroma(key[None].astype(np.uint8))[0], axis=1).std())
    return key, noise


def refine_alpha(alpha: np.ndarray, erode: float = 0.0, feather: float = 0.0) -> np.ndarray:
    """Choke and soften a matte: `erode` pulls the edge in by that many pixels (removes the key's fringe and
    any spill a soft edge still carries), `feather` blurs the new edge by that many pixels. Float alpha 0..1."""
    if erode > 0:
        r = int(round(erode))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        alpha = cv2.erode(alpha, kernel)
    if feather > 0:
        alpha = np.minimum(alpha, cv2.GaussianBlur(alpha, (0, 0), feather)) if erode > 0 else \
            cv2.GaussianBlur(alpha, (0, 0), feather)
    return np.clip(alpha, 0.0, 1.0)


def key_frame(rgb: np.ndarray, key: np.ndarray, noise: float, erode: float = 0.0, feather: float = 0.0) -> np.ndarray:
    """RGBA from one frame: alpha from chroma distance to the key, colour unmixed, spill removed, then the
    matte choked (`erode`) and softened (`feather`) when asked."""
    kc = _chroma(key[None].astype(np.uint8))[0]
    d = np.linalg.norm(_chroma(rgb) - kc, axis=-1)
    lo = 6.0 + 3.0 * noise
    hi = lo + 34.0
    alpha = np.clip((d - lo) / (hi - lo), 0.0, 1.0)
    # remove isolated speckles in the background and pinholes in the subject
    a8 = (alpha * 255).astype(np.uint8)
    a8 = cv2.morphologyEx(a8, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    a8 = cv2.morphologyEx(a8, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    alpha = a8.astype(np.float32) / 255.0
    f = rgb.astype(np.float32)
    k = key.astype(np.float32)
    a = np.maximum(alpha[..., None], 1e-3)
    fg = np.clip((f - (1.0 - alpha[..., None]) * k) / a, 0, 255)
    # spill: near the edge, the key's dominant channel may not exceed the larger of the other two
    order = np.argsort(k)[::-1]
    if k[order[0]] - k[order[1]] > 60:                    # a single-channel key (green, blue)
        dom, others = int(order[0]), [int(c) for c in order[1:]]
        near_edge = cv2.dilate((alpha < 0.999).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        cap = np.max(fg[..., others], axis=-1)
        fg[..., dom] = np.where(near_edge, np.minimum(fg[..., dom], cap), fg[..., dom])
    if erode > 0 or feather > 0:
        alpha = refine_alpha(alpha, erode, feather)
    return np.dstack([fg, alpha * 255.0]).astype(np.uint8)


def matte_metrics(rgba: np.ndarray) -> dict[str, float]:
    """Interior holes (alpha < 0.5 inside the subject's filled outline) and soft-edge share."""
    a = rgba[..., 3]
    solid = (a > 127).astype(np.uint8)
    if solid.sum() < 50:
        return {"coverage": 0.0, "holes": 0.0, "soft": 0.0}
    cnts, _ = cv2.findContours(solid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(solid)
    cv2.drawContours(filled, cnts, -1, 1, thickness=-1)
    holes = float(((filled == 1) & (solid == 0)).sum() / max(1, filled.sum()))
    soft = float(((a > 5) & (a < 250)).sum() / max(1, (a > 5).sum()))
    return {"coverage": round(float(solid.mean()), 4), "holes": round(holes, 4), "soft": round(soft, 4)}


def key_clip(src: Path, out: Path, preview_frames: int = 6, erode: float = 0.0, feather: float = 0.0,
             expected: tuple[int, int, int] | None = None) -> dict[str, Any]:
    """Key every frame of src (any video) to RGBA, write ProRes 4444 with alpha, return metrics and
    a few keyed frames for previews. `erode`/`feather` (pixels of src) choke and soften every frame's matte."""
    probe = subprocess.run([which("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True, check=True).stdout.strip().split(",")
    w, h = int(probe[0]), int(probe[1])
    num, den = (int(x) for x in probe[2].split("/"))
    fps = num / den
    dec = subprocess.Popen([which("ffmpeg"), "-v", "error", "-i", str(src), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    out.parent.mkdir(parents=True, exist_ok=True)
    enc = subprocess.Popen([which("ffmpeg"), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}",
                            "-r", f"{num}/{den}", "-i", "-", "-c:v", "prores_ks", "-profile:v", "4444",
                            "-pix_fmt", "yuva444p10le", "-alpha_bits", "16", str(out)], stdin=subprocess.PIPE)
    assert dec.stdout is not None and enc.stdin is not None
    frames: list[np.ndarray] = []
    per: list[dict[str, float]] = []
    keys: list[list[float]] = []
    n = 0
    try:
        while True:
            buf = dec.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            rgb = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            key, noise = background_key(rgb, expected=expected)
            rgba = key_frame(rgb, key, noise, erode, feather)
            enc.stdin.write(rgba.tobytes())
            frames.append(rgba)
            keys.append([round(float(v), 1) for v in key])
            if n % 6 == 0:
                per.append({**matte_metrics(rgba), "noise": round(noise, 2)})
            n += 1
    finally:
        enc.stdin.close()
        enc.wait()
        dec.stdout.close()
        dec.wait()
    if enc.returncode != 0 or n == 0:
        raise RuntimeError(f"keying {src.name} failed")
    pick = [frames[int(i * (n - 1) / max(1, preview_frames - 1))] for i in range(preview_frames)]
    metrics = {"frames": n, "fps": round(fps, 3), "size": [w, h],
               "holes_max": max(p["holes"] for p in per), "soft_mean": round(float(np.mean([p["soft"] for p in per])), 4),
               "coverage_mean": round(float(np.mean([p["coverage"] for p in per])), 4),
               "key_noise_max": max(p["noise"] for p in per), "key_first": keys[0], "key_last": keys[-1],
               "erode": erode, "feather": feather}
    return {"metrics": metrics, "preview": pick}
