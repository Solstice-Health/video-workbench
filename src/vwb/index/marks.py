"""Mark mining: persistent graphics (logos, footers, page furniture) and their best instance.

A region that is foreground in most shots and barely changes between them is page
furniture: a logo, a job code, a page number. For each candidate we search every shot for
the largest clean instance (multi-scale template match). Rung 0 of the asset lab recovers
the mark from that instance (spec: "largest, lowest QP, unoccluded").
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from vwb.index.layout import background_color


def persistent_regions(reps: list[np.ndarray], min_frac: float = 0.6) -> list[dict[str, Any]]:
    """Candidates from per-shot representative frames (all the same size)."""
    if len(reps) < 3:
        return []
    h, w = reps[0].shape[:2]
    fg = []
    for rgb in reps:
        bg, uniform = background_color(rgb)
        if bg is None or uniform < 0.55:
            fg.append(np.zeros((h, w), bool))
            continue
        fg.append(np.max(np.abs(rgb.astype(np.int16) - bg.astype(np.int16)), axis=2) > 28)
    fg_frac = np.mean(np.stack(fg), axis=0)
    gray = np.stack([cv2.cvtColor(r, cv2.COLOR_RGB2GRAY) for r in reps]).astype(np.float32)
    med = np.median(gray, axis=0)
    mad = np.median(np.abs(gray - med), axis=0)
    mask = ((fg_frac >= min_frac) & (mad < 10)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 9)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 40 or bw * bh > 0.08 * w * h:
            continue
        crop_fg = fg_frac[y:y + bh, x:x + bw]
        out.append({"x": float(x), "y": float(y), "w": float(bw), "h": float(bh),
                    "persistence": float(np.mean(crop_fg[crop_fg > 0])) if np.any(crop_fg > 0) else 0.0})
    return out


def best_instance(template: np.ndarray, frames: dict[int, np.ndarray], scales: list[float] | None = None,
                  min_score: float = 0.72) -> dict[str, Any] | None:
    """Largest instance of `template` (RGB) across frames. Returns frame, box and score."""
    scales = scales or [0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
    t_gray = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)
    th, tw = t_gray.shape
    best = None
    for idx, rgb in frames.items():
        g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        for s in scales:
            tws, ths = int(round(tw * s)), int(round(th * s))
            if tws < 12 or ths < 8 or tws >= g.shape[1] or ths >= g.shape[0]:
                continue
            t = cv2.resize(t_gray, (tws, ths), interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)
            if float(t.std()) < 4:
                continue
            res = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if score < min_score:
                continue
            area = tws * ths
            key = (area * score)
            if best is None or key > best["key"]:
                best = {"frame": int(idx), "box": [float(loc[0]), float(loc[1]), float(tws), float(ths)],
                        "score": float(score), "scale": float(s), "key": key}
    if best:
        best.pop("key")
    return best
