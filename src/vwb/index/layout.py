"""Layout blocks: the content units of a shot (text blocks, graphics, people).

On a flat background (slides, motion graphics, end cards) we segment foreground from the
background colour and group nearby marks into blocks. The reframer uses blocks to decide
what a camera window must contain. On natural footage there is no flat background, so we
return no blocks and the reframer falls back to faces and saliency.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


def background_color(rgb: np.ndarray, band: float = 0.03) -> tuple[np.ndarray | None, float]:
    """Dominant border colour and how uniform the border is (0..1)."""
    h, w = rgb.shape[:2]
    bh, bw = max(2, int(h * band)), max(2, int(w * band))
    border = np.concatenate([rgb[:bh].reshape(-1, 3), rgb[-bh:].reshape(-1, 3),
                             rgb[:, :bw].reshape(-1, 3), rgb[:, -bw:].reshape(-1, 3)])
    q = (border // 16).astype(np.int32)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    mode = np.bincount(keys).argmax()
    sel = border[keys == mode]
    color = np.median(sel, axis=0)
    dist = np.max(np.abs(border.astype(np.int16) - color.astype(np.int16)), axis=1)
    return color.astype(np.uint8), float(np.mean(dist < 24))


def segment(rgb: np.ndarray, text_boxes: list[tuple[float, float, float, float]],
            face_boxes: list[tuple[float, float, float, float]], depth: int = 0) -> dict[str, Any]:
    """Blocks of one frame. A large block on its own flat panel is segmented again inside
    (one level), so a chart and a text box sharing a grey panel come apart."""
    res = _segment_flat(rgb, text_boxes, face_boxes)
    if depth > 0 or not res["blocks"]:
        return res
    H, W = rgb.shape[:2]
    out = []
    for b in res["blocks"]:
        if b["area"] < 0.18:
            out.append(b)
            continue
        x, y, bw, bh = (int(round(v)) for v in (b["x"], b["y"], b["w"], b["h"]))
        sub_rgb = rgb[y:y + bh, x:x + bw]
        sub_text = [(t[0] - x, t[1] - y, t[2], t[3]) for t in text_boxes if _center_in(t, (x, y, bw, bh))]
        sub_faces = [(f[0] - x, f[1] - y, f[2], f[3]) for f in face_boxes if _center_in(f, (x, y, bw, bh))]
        inner = _segment_flat(sub_rgb, sub_text, sub_faces, min_area_abs=0.0012 * W * H)
        if len(inner["blocks"]) >= 2:
            for ib in inner["blocks"]:
                ib.update(x=ib["x"] + x, y=ib["y"] + y, area=ib["w"] * ib["h"] / (W * H), parent_area=b["area"])
                out.append(ib)
        else:
            out.append(b)
    out.sort(key=lambda b: (b["y"], b["x"]))
    res["blocks"] = _dedupe(out)
    return res


def _dedupe(blocks: list[dict[str, Any]], thresh: float = 0.85) -> list[dict[str, Any]]:
    """Drop blocks that are near-copies of an earlier one (recursion finds some twice)."""
    kept: list[dict[str, Any]] = []
    for b in blocks:
        bb = (b["x"], b["y"], b["w"], b["h"])
        if any(_iou(bb, (k["x"], k["y"], k["w"], k["h"])) > thresh for k in kept):
            continue
        kept.append(b)
    return kept


def _iou(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _segment_flat(rgb: np.ndarray, text_boxes: list[tuple[float, float, float, float]],
                  face_boxes: list[tuple[float, float, float, float]], min_area_abs: float | None = None) -> dict[str, Any]:
    h, w = rgb.shape[:2]
    bg, uniform = background_color(rgb)
    if bg is None or uniform < 0.55:
        return {"bg": None, "uniform": uniform, "blocks": []}
    diff = np.max(np.abs(rgb.astype(np.int16) - bg.astype(np.int16)), axis=2)
    mask = (diff > 28).astype(np.uint8)
    # merge glyphs into lines and lines into paragraphs, but keep columns apart
    kx, ky = max(9, int(w * 0.016)) | 1, max(9, int(h * 0.03)) | 1
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    blocks = []
    min_area = min_area_abs if min_area_abs is not None else 0.0012 * w * h
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw * bh < min_area:
            continue
        if bw > 0.92 * w and bh > 0.85 * h:
            continue  # a full-frame panel, not a content unit
        box = (float(x), float(y), float(bw), float(bh))
        inside_text = [t for t in text_boxes if _center_in(t, box)]
        text_area = sum(t[2] * t[3] for t in inside_text)
        has_face = any(_overlap_frac(f, box) > 0.5 for f in face_boxes)
        if has_face:
            kind = "person"
        elif text_area >= 0.30 * bw * bh:
            kind = "text"
        elif inside_text:
            kind = "mixed"
        else:
            kind = "graphic"
        blocks.append({"x": box[0], "y": box[1], "w": box[2], "h": box[3], "kind": kind,
                       "area": float(bw * bh) / (w * h), "n_text": len(inside_text)})
    blocks.sort(key=lambda b: (b["y"], b["x"]))
    return {"bg": bg.tolist(), "uniform": uniform, "blocks": blocks}


def _center_in(inner, outer) -> bool:
    cx, cy = inner[0] + inner[2] / 2, inner[1] + inner[3] / 2
    return outer[0] <= cx <= outer[0] + outer[2] and outer[1] <= cy <= outer[1] + outer[3]


def _overlap_frac(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return inter / max(1e-6, a[2] * a[3])
