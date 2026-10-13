"""Trace a raster element (icon, logo, flat illustration) into colour vector paths (VTracer).

A traced element renders crisply at any size, so a 100 px source icon can fill half the frame
without the blur of an upscale, and its colours can be changed to the set's palette. The
result is measured against the raster it came from: the silhouette overlap (IoU) and the
colour error of the rasterised paths. Provenance class: traced (GEN-RECOVERED:trace when an
output uses it), since the shapes are re-drawn as curves.
"""

from __future__ import annotations

import re
from typing import Any

import cv2
import numpy as np
import vtracer

PATH_RE = re.compile(r'<path\s+d="([^"]+)"\s+fill="(#[0-9A-Fa-f]{6})"(?:\s+transform="translate\(([-\d.]+),\s*([-\d.]+)\)")?')
TOKEN = re.compile(r"[MLCQZmlcqz]|-?\d+(?:\.\d+)?(?:e-?\d+)?")


def quantize(rgba: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Reduce the opaque pixels to k colours (k-means), then clean single-pixel speckles: anti-aliased
    edges otherwise become dozens of near-identical colour layers and ragged outlines."""
    out = rgba.copy()
    a = out[..., 3] >= 128
    px = out[..., :3][a].astype(np.float32)
    if len(px) < k * 4:
        return out, np.unique(px.astype(np.uint8), axis=0)
    k = int(min(k, len(np.unique((px // 8).astype(np.int32), axis=0))))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    _, labels, centers = cv2.kmeans(px, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    lab = np.full(a.shape, 255, np.uint8)
    lab[a] = labels.flatten().astype(np.uint8)
    # colours that live on the silhouette's edge are anti-aliasing blends, not real colours: merge them away
    edge = a & cv2.dilate((~a).astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    real = [c for c in range(k) if (lab[edge] == c).sum() < 0.6 * max(1, (lab[a] == c).sum())]
    if real and len(real) < k:
        for c in set(range(k)) - set(real):
            near = min(real, key=lambda r: float(np.abs(centers[r] - centers[c]).sum()))
            lab[lab == c] = near
    lab = cv2.medianBlur(lab, 5)
    keep = lab != 255
    out[..., 3] = np.where(keep, 255, 0)
    out[..., :3][keep] = centers.astype(np.uint8)[lab[keep]]
    return out, centers.astype(np.uint8)


def trace(rgba: np.ndarray, *, detail: str = "medium", upscale: float | None = None, max_colors: int = 16) -> dict[str, Any]:
    """Vector paths of an RGBA image. detail: low | medium | high (colours kept, speckle filter)."""
    h, w = rgba.shape[:2]
    k = upscale or float(np.clip(640 / max(h, w), 1.0, 4.0))          # trace small art at a larger size
    img = cv2.resize(rgba, (round(w * k), round(h * k)), interpolation=cv2.INTER_CUBIC) if k != 1 else rgba
    img = img.copy()
    img[img[..., 3] < 128] = 0                                        # hard alpha: VTracer keys out transparency
    img[..., 3] = np.where(img[..., 3] >= 128, 255, 0)
    img, palette = quantize(img, {"low": 3, "medium": 5, "high": 8}[detail])
    p = {"low": (16, 8, 32), "medium": (10, 8, 24), "high": (6, 8, 16)}[detail]
    H, W = img.shape[:2]
    svg = vtracer.convert_pixels_to_svg([tuple(int(v) for v in px) for px in img.reshape(-1, 4)], (W, H),
                                        colormode="color", hierarchical="stacked", mode="spline",
                                        filter_speckle=p[0], color_precision=p[1], layer_difference=p[2],
                                        corner_threshold=60, length_threshold=4.0, splice_threshold=45,
                                        path_precision=2)
    paths = []
    pal = palette.astype(np.int32)
    for d, fill, tx, ty in PATH_RE.findall(svg):
        c = np.array([int(fill[i:i + 2], 16) for i in (1, 3, 5)])
        snap = pal[np.argmin(np.abs(pal - c).sum(axis=1))]               # exact palette colours, no stacked blends
        paths.append({"d": d, "fill": "#%02X%02X%02X" % tuple(int(v) for v in snap),
                      "translate": [float(tx or 0), float(ty or 0)]})
    if len(paths) > 400:                                               # keep the largest shapes
        paths = paths[:400]
    colours = sorted({p_["fill"] for p_ in paths})
    return {"paths": paths, "viewbox": [W, H], "scale": k, "svg": _svg(paths, W, H), "colours": colours[:max_colors]}


def _svg(paths: list[dict[str, Any]], W: int, H: int) -> str:
    body = "".join(f'<path d="{p["d"]}" fill="{p["fill"]}" transform="translate({p["translate"][0]},{p["translate"][1]})"/>'
                   for p in paths)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">{body}</svg>'


def _flatten(d: str, tx: float, ty: float, steps: int = 8) -> list[np.ndarray]:
    """Polygons of an SVG path (M, L, C, Q, Z, absolute and relative) for rasterising previews."""
    toks = TOKEN.findall(d)
    polys, cur, x, y, sx, sy, i, cmd = [], [], 0.0, 0.0, 0.0, 0.0, 0, "M"

    def num() -> float:
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if cur:
                    polys.append(np.array(cur))
                cur, x, y = [], sx, sy
                continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            if cur:
                polys.append(np.array(cur))
            cur, sx, sy = [(x, y)], x, y
            cmd = "l" if rel else "L"
        elif c == "L":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            cur.append((x, y))
        elif c in "CQ":
            n = 3 if c == "C" else 2
            pts = []
            for _ in range(n):
                px, py = num(), num()
                pts.append((x + px, y + py) if rel else (px, py))
            p0 = np.array([x, y])
            ctrl = [np.array(p) for p in pts]
            for s in range(1, steps + 1):
                t = s / steps
                if c == "C":
                    b = ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * ctrl[0] + 3 * (1 - t) * t * t * ctrl[1] + t ** 3 * ctrl[2]
                else:
                    b = ((1 - t) ** 2) * p0 + 2 * (1 - t) * t * ctrl[0] + t * t * ctrl[1]
                cur.append((float(b[0]), float(b[1])))
            x, y = pts[-1]
        else:
            i += 1
    if cur:
        polys.append(np.array(cur))
    return [p + np.array([tx, ty]) for p in polys if len(p) >= 3]


def rasterize(paths: list[dict[str, Any]], viewbox: list[float], size: tuple[int, int]) -> np.ndarray:
    """RGBA preview of traced paths at size (w, h), painted in order (stacked)."""
    w, h = size
    img = np.zeros((h, w, 4), np.uint8)
    sx, sy = w / viewbox[0], h / viewbox[1]
    for p in paths:
        col = tuple(int(p["fill"][i:i + 2], 16) for i in (1, 3, 5))
        polys = [np.round(poly * [sx, sy]).astype(np.int32) for poly in _flatten(p["d"], *p["translate"])]
        if polys:
            mask = np.zeros((h, w), np.uint8)
            cv2.fillPoly(mask, polys, 255, lineType=cv2.LINE_AA)   # even-odd per path: holes stay open
            m = mask > 127
            img[m, :3] = col
            img[m, 3] = 255
    return img


def fidelity(original: np.ndarray, paths: list[dict[str, Any]], viewbox: list[float]) -> dict[str, float]:
    h, w = original.shape[:2]
    r = rasterize(paths, viewbox, (w, h))
    a, b = original[..., 3] > 127, r[..., 3] > 127
    iou = float((a & b).sum() / max(1, (a | b).sum()))
    both = a & b
    de = float(np.abs(original[..., :3][both].astype(int) - r[..., :3][both].astype(int)).mean()) if both.any() else 0.0
    return {"iou": round(iou, 4), "colour_error": round(de, 2)}
