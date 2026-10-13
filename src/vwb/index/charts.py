"""Measure a bar chart in an image: bars, the value axis, category labels and the legend.

Unlabelled source charts can still be recreated (flat, 3D, animated) if their values are
measured instead of guessed. This reads the chart deterministically:
  bars    rectangle-like regions of one fill colour (connected components per colour cluster)
  axis    OCR'd tick numbers along the value axis, fitted to pixel positions (least squares);
          the fit's residual says how far to trust it
  values  each bar's end mapped through the fit; category labels from the text beside the
          bar groups; legend entries from text next to small swatches of a bar colour
A measured value is an estimate with the chart's precision: it is typeset as authored copy
and flagged for a person to confirm.
"""

from __future__ import annotations

import re
from typing import Any

import cv2
import numpy as np

from vwb.index import vision
from vwb.index.layout import background_color

NUM = re.compile(r"^[<>≤≥~]?\s*-?\d[\d,]*(\.\d+)?\s*%?$")


def _num(text: str) -> float | None:
    t = text.strip().replace(" ", "")
    if not NUM.match(t):
        return None
    try:
        return float(re.sub(r"[^\d.\-]", "", t))
    except ValueError:
        return None


def _colours(px: np.ndarray, max_k: int = 8) -> list[np.ndarray]:
    """Dominant fill colours: quantised histogram peaks, merged when close."""
    q = (px // 24).astype(np.int32)
    keys, counts = np.unique(q[:, 0] * 10000 + q[:, 1] * 100 + q[:, 2], return_counts=True)
    order = np.argsort(counts)[::-1]
    out: list[np.ndarray] = []
    for k in order[:40]:
        if counts[k] < max(40, 0.004 * len(px)):
            break
        sel = px[(q[:, 0] * 10000 + q[:, 1] * 100 + q[:, 2]) == keys[k]]
        c = np.median(sel, axis=0)
        if all(np.abs(c - o).max() > 28 for o in out):
            out.append(c)
        if len(out) >= max_k:
            break
    return out


def read_chart(rgb: np.ndarray, box: list[float], orientation: str = "auto") -> dict[str, Any]:
    H, W = rgb.shape[:2]
    x0, y0 = int(max(0, box[0])), int(max(0, box[1]))
    x1, y1 = int(min(W, box[0] + box[2])), int(min(H, box[1] + box[3]))
    crop = np.ascontiguousarray(rgb[y0:y1, x0:x1, :3])
    ch, cw = crop.shape[:2]
    bg, _ = background_color(crop)
    dist = np.abs(crop.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    sat = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)[..., 1]
    fill = (dist > 40) & (sat > 50)
    colours = _colours(crop[fill]) if fill.any() else []
    bars, swatches = [], []
    for ci, col in enumerate(colours):
        m = (np.abs(crop.astype(np.int16) - col.astype(np.int16)).max(axis=2) < 26) & fill
        m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))   # drops thin lines over bars
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=4)
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            if area < 30 or area / max(1, w * h) < 0.8:
                continue
            item = {"colour": "#%02X%02X%02X" % tuple(int(v) for v in col), "colour_id": ci,
                    "box": [x + x0, y + y0, w, h]}
            # a legend swatch is small and squarish; a bar is long in one direction
            if max(w, h) < 0.06 * max(cw, ch) and 0.5 < w / max(1, h) < 2.0:
                swatches.append(item)
            elif area >= max(60, 0.0003 * cw * ch):
                bars.append(item)
    if orientation == "auto":
        tall = sum(1 for b in bars if b["box"][3] >= b["box"][2])
        orientation = "vertical" if tall >= len(bars) / 2 else "horizontal"
    lines = vision.analyze(crop, ocr=True, faces=False, humans=False, saliency=False).lines
    texts = [{"text": ln.text, "box": [ln.box[0] + x0, ln.box[1] + y0, ln.box[2], ln.box[3]]} for ln in lines if ln.conf >= 0.3]
    # value axis: numeric labels beside the bars (left of them for vertical bars, below for horizontal)
    if bars:
        bx0 = min(b["box"][0] for b in bars)
        by1 = max(b["box"][1] + b["box"][3] for b in bars)
    else:
        bx0, by1 = x1, y0
    ticks = []
    for t in texts:
        v = _num(t["text"])
        if v is None:
            continue
        cx, cy = t["box"][0] + t["box"][2] / 2, t["box"][1] + t["box"][3] / 2
        if orientation == "vertical" and t["box"][0] + t["box"][2] <= bx0 + 4:
            ticks.append((v, cy))
        elif orientation == "horizontal" and t["box"][1] >= by1 - 4:
            ticks.append((v, cx))
    fit = None
    if len(ticks) >= 2:
        vals, pos = np.array([t[0] for t in ticks]), np.array([t[1] for t in ticks])
        A = np.vstack([pos, np.ones_like(pos)]).T
        (b, a), *_ = np.linalg.lstsq(A, vals, rcond=None)
        pred = A @ np.array([b, a])
        ss = float(((vals - vals.mean()) ** 2).sum()) or 1.0
        r2 = 1 - float(((vals - pred) ** 2).sum()) / ss
        unit = abs(b)                                   # value per pixel
        fit = {"value_per_px": round(float(b), 6), "value_at_0px": round(float(a), 4), "r2": round(r2, 5),
               "ticks": [{"value": v, "px": round(p, 1)} for v, p in ticks], "precision": round(unit * 1.5, 4)}
    for bar in bars:
        x, y, w, h = bar["box"]
        end = y if orientation == "vertical" else x + w   # the bar's free end
        if fit:
            bar["value"] = round(fit["value_per_px"] * end + fit["value_at_0px"], 3)
        # category label: the nearest text across the base from the bar's middle
        mid = x + w / 2 if orientation == "vertical" else y + h / 2
        cands = [t for t in texts if _num(t["text"]) is None and (
            (orientation == "vertical" and t["box"][1] >= y + h - 2) or (orientation == "horizontal" and t["box"][0] + t["box"][2] <= x + 2))]
        if cands:
            axis_i = 0 if orientation == "vertical" else 1
            bar["category"] = min(cands, key=lambda t, m=mid, i=axis_i: abs(t["box"][i] + t["box"][i + 2] / 2 - m))["text"]
    legend = []
    for sw in swatches:
        sx, sy, sw_, sh = sw["box"]
        # the label starts at the swatch (OCR often reads the swatch as a bullet) or just right of it
        near = [t for t in texts if sx - 4 <= t["box"][0] <= sx + sw_ + 3 * max(sw_, 10)
                and abs(t["box"][1] + t["box"][3] / 2 - (sy + sh / 2)) < max(sh, 12) and _num(t["text"]) is None]
        if near:
            t = min(near, key=lambda t: abs(t["box"][0] - (sx + sw_)))
            legend.append({"colour": sw["colour"], "colour_id": sw["colour_id"], "text": t["text"].lstrip("•■●▪◼ ").strip()})
    series = {e["colour_id"]: e["text"] for e in legend}
    for bar in bars:
        if bar["colour_id"] in series:
            bar["series"] = series[bar["colour_id"]]
    key = (lambda b: (b["box"][0], b["box"][1])) if orientation == "vertical" else (lambda b: (b["box"][1], b["box"][0]))
    bars.sort(key=key)
    return {"orientation": orientation, "box": [x0, y0, x1 - x0, y1 - y0], "background": "#%02X%02X%02X" % tuple(int(v) for v in bg),
            "bars": bars, "axis": fit, "legend": legend, "texts": [t["text"] for t in texts]}


def annotate(rgb: np.ndarray, result: dict[str, Any]) -> np.ndarray:
    x0, y0, w, h = result["box"]
    img = np.ascontiguousarray(rgb[y0:y0 + h, x0:x0 + w, :3]).copy()
    for i, b in enumerate(result["bars"]):
        x, y, bw, bh = b["box"]
        cv2.rectangle(img, (x - x0, y - y0), (x - x0 + bw, y - y0 + bh), (255, 0, 80), 2)
        label = f"{i}:{b.get('value', '?')}"
        cv2.putText(img, label, (x - x0, max(12, y - y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 80), 1, cv2.LINE_AA)
    return img
