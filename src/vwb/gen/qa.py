"""Measurements carried by every generated asset (they become verifier evidence).

A generative model can draw wrong text, invent a person, leave a halo, or shift what it was
told to keep. These checks are cheap and run on every result:
  alpha_stats   coverage, soft-edge share, and whether the subject touches the border
  read_text     strings found by OCR (generated pixels must not carry text the copy deck lacks)
  faces         faces found (a generated person is SYNTH-PERSON)
  seam          how well generated pixels continue the kept ones along a mask edge
  fidelity      how close an extracted element is to the source pixels it came from
"""

from __future__ import annotations

import re
from typing import Any

import cv2
import numpy as np
from PIL import Image

from vwb.index import vision


def alpha_stats(rgba: np.ndarray) -> dict[str, Any]:
    a = rgba[..., 3].astype(np.float32) / 255.0
    solid = a > 0.98
    soft = (a > 0.02) & ~solid
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    ys, xs = np.nonzero(a > 0.5)
    bbox = [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)] if xs.size else None
    return {"coverage": round(float((a > 0.5).mean()), 4), "soft_share": round(float(soft.sum() / max(1, (a > 0.02).sum())), 4),
            "touches_border": bool((edge > 0.5).mean() > 0.02), "bbox": bbox,
            "transparent": bool((a < 0.02).mean() > 0.02)}


def _on_white(rgba: np.ndarray) -> np.ndarray:
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    return (rgba[..., :3].astype(np.float32) * a + 255.0 * (1 - a)).astype(np.uint8)


def read_text(img: np.ndarray, min_conf: float = 0.5) -> list[str]:
    """OCR strings in an image (RGBA is flattened on white). Apple Vision, accurate, no correction."""
    rgb = _on_white(img) if img.shape[2] == 4 else img
    h, w = rgb.shape[:2]
    if max(h, w) < 900:                      # OCR reads small renders better when upscaled
        k = 900 / max(h, w)
        rgb = cv2.resize(rgb, (round(w * k), round(h * k)), interpolation=cv2.INTER_CUBIC)
    res = vision.analyze(rgb, ocr=True, faces=False, humans=False, saliency=False)
    # shapes in illustrations (branches, ribs) read as stray glyphs; real text has a few letters or digits
    return [ln.text for ln in res.lines if ln.conf >= min_conf and len(re.sub(r"[^A-Za-z0-9]", "", ln.text)) >= 3]


def faces(img: np.ndarray) -> int:
    rgb = _on_white(img) if img.shape[2] == 4 else img
    return len(vision.analyze(rgb, ocr=False, faces=True, humans=False, saliency=False).faces)


def seam(generated: np.ndarray, original: np.ndarray, keep: np.ndarray, band: int = 6) -> dict[str, float]:
    """Compare generated and original pixels in a thin band just inside the kept region's edge.
    If the model kept the content in place, the two agree there (low error)."""
    keep_u8 = (keep > 0.5).astype(np.uint8)
    inner = cv2.erode(keep_u8, np.ones((band, band), np.uint8))
    ring = (keep_u8 - inner).astype(bool)
    if ring.sum() < 50:
        return {"mae": 0.0, "p95": 0.0, "n": int(ring.sum())}
    d = np.abs(generated[..., :3].astype(np.int16) - original[..., :3].astype(np.int16)).max(axis=2)[ring]
    return {"mae": round(float(d.mean()), 2), "p95": round(float(np.percentile(d, 95)), 2), "n": int(ring.sum())}


def register(cut: np.ndarray, source: np.ndarray, min_inliers: int = 12) -> dict[str, Any] | None:
    """Where a cut-out sits in the source, as a similarity transform (scale, rotation, shift) found by
    SIFT matches and RANSAC. None when the element cannot be found (too few consistent matches)."""
    a = cut[..., 3]
    gray_cut = cv2.cvtColor(_on_white(cut), cv2.COLOR_RGB2GRAY)
    gray_src = cv2.cvtColor(np.ascontiguousarray(source[..., :3]), cv2.COLOR_RGB2GRAY)
    sift = cv2.SIFT_create(nfeatures=4000)
    k1, d1 = sift.detectAndCompute(gray_cut, (a > 128).astype(np.uint8) * 255)
    k2, d2 = sift.detectAndCompute(gray_src, None)
    if d1 is None or d2 is None or len(k1) < min_inliers or len(k2) < min_inliers:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(d1, d2, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < min_inliers:
        return None
    src_pts = np.float32([k1[m.queryIdx].pt for m in good])
    dst_pts = np.float32([k2[m.trainIdx].pt for m in good])
    M, inl = cv2.estimateAffinePartial2D(src_pts, dst_pts, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if M is None or int(inl.sum()) < min_inliers:
        return None
    scale = float(np.hypot(M[0, 0], M[1, 0]))
    angle = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
    return {"M": M, "scale": round(scale, 4), "rotation_deg": round(angle, 2), "inliers": int(inl.sum()), "matches": len(good)}


def fidelity(cut: np.ndarray, source: np.ndarray) -> dict[str, Any]:
    """How faithful an extracted element is to the source pixels it came from: register it, warp it
    into the source frame, and take the PSNR under its alpha. Also returns where it sits in the source.
    Low PSNR (or no registration) means the element was redrawn, not lifted."""
    reg = register(cut, source)
    if reg is None:
        return {"registered": False, "psnr": None, "note": "the element could not be located in the source"}
    H, W = source.shape[:2]
    warped = cv2.warpAffine(cut, reg["M"], (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                            borderValue=(0, 0, 0, 0))
    m = warped[..., 3] > 200
    if m.sum() < 100:
        return {"registered": False, "psnr": None, "note": "the element maps outside the source"}
    diff = warped[..., :3][m].astype(np.float32) - source[..., :3][m].astype(np.float32)
    mse = float((diff ** 2).mean())
    psnr = 99.0 if mse <= 1e-6 else 10 * np.log10(255.0 ** 2 / mse)
    ys, xs = np.nonzero(warped[..., 3] > 127)
    placed = [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
    return {"registered": True, "psnr": round(float(psnr), 2), "scale": reg["scale"], "rotation_deg": reg["rotation_deg"],
            "inliers": reg["inliers"], "placed": placed}


def checkerboard(rgba: np.ndarray, cell: int = 16) -> np.ndarray:
    """RGBA over a light checkerboard, for looking at alpha."""
    h, w = rgba.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    board = np.where(((yy // cell + xx // cell) % 2)[..., None] == 0, 236, 200).astype(np.float32)
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    return (rgba[..., :3].astype(np.float32) * a + board * (1 - a)).astype(np.uint8)


def preview(images: list[np.ndarray], height: int = 640, gap: int = 12) -> np.ndarray:
    """Side-by-side preview (alpha shown on a checkerboard)."""
    tiles = []
    for im in images:
        rgb = checkerboard(im) if im.shape[2] == 4 else im
        k = height / rgb.shape[0]
        tiles.append(np.asarray(Image.fromarray(rgb).resize((max(1, round(rgb.shape[1] * k)), height), Image.LANCZOS)))
    out = np.full((height, sum(t.shape[1] for t in tiles) + gap * (len(tiles) - 1), 3), 40, np.uint8)
    x = 0
    for t in tiles:
        out[:, x:x + t.shape[1]] = t
        x += t.shape[1] + gap
    return out
