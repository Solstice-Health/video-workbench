"""Geometry generators for data graphics (like d3's shape helpers): layers the agent pastes into a canvas.

Hand-computing isometric faces, arc paths or pictogram grids is error-prone for a language model; these
return correct `shape` layers from a few parameters. They make geometry, not layouts: where a graphic
sits, its values, colours and timing are the caller's.

  iso_bars    isometric 3D bars (front, side, top faces) for values on a shared scale, optionally growing
              one after another
  ring        a progress ring (track and value arc) that can fill over time
  pictogram   a grid of dots or squares, the first `filled` ones highlighted, optionally filling over time
  arrow       a straight arrow with a head, optionally drawn over time
"""

from __future__ import annotations

import math
from typing import Any


def _shade(hex_color: str, k: float) -> str:
    v = hex_color.lstrip("#")
    r, g, b = (int(v[i:i + 2], 16) for i in (0, 2, 4))
    f = (lambda c: min(255, int(c + (255 - c) * (k - 1)))) if k > 1 else (lambda c: int(c * k))
    return "#%02X%02X%02X" % (f(r), f(g), f(b))


def iso_bars(prefix: str, box: list[float], values: list[float], *, max_value: float | None = None,
             colors: list[str] | str = "#2E5BFF", depth: float = 0.35, gap: float = 0.35, start: int = 0,
             stagger: int = 12, grow: int = 24, span: list[int] | None = None) -> list[dict[str, Any]]:
    """Bars stand on the bottom edge of `box`; each bar is a group of three faces in its own layer whose box
    grows from the baseline (front darkest-mid, side darker, top lighter)."""
    x, y, w, h = box
    n = max(1, len(values))
    top = max_value or max(values) or 1.0
    unit = w / (n + (n - 1) * gap)
    bw = unit                                    # front face width
    dx = bw * depth                              # isometric side width
    dy = dx * 0.5
    layers = []
    cols = [colors] * n if isinstance(colors, str) else colors
    for i, v in enumerate(values):
        bh = max(1.0, (h - dy) * float(v) / top)
        bx = x + i * unit * (1 + gap)
        c = cols[i % len(cols)]
        # local coordinates: viewbox = (bw + dx) x (bh + dy); front at bottom-left
        vw, vh = bw + dx, bh + dy
        front = f"M0 {dy} L{bw} {dy} L{bw} {vh} L0 {vh} Z"
        side = f"M{bw} {dy} L{vw} 0 L{vw} {bh} L{bw} {vh} Z"
        topf = f"M0 {dy} L{dx} 0 L{vw} 0 L{bw} {dy} Z"
        final = [round(bx, 1), round(y + h - vh, 1), round(vw, 1), round(vh, 1)]
        t0 = start + i * stagger
        layer = {"id": f"{prefix}-{i}", "type": "shape", "shape": "path", "role": "content", "viewbox": [round(vw, 2), round(vh, 2)],
                 "paths": [{"d": front, "fill": c}, {"d": side, "fill": _shade(c, 0.62)}, {"d": topf, "fill": _shade(c, 1.35)}],
                 "box": [{"f": t0, "v": [final[0], round(y + h - 1, 1), final[2], 1]},
                         {"f": t0 + grow, "v": final, "ease": "out"}] if grow else final,
                 "note": f"isometric bar {i}: value {v} of {top}"}
        if span:
            layer["span"] = span
        layers.append(layer)
    return layers


def _arc(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    """SVG arc path from angle a0 to a1 (degrees, 0 = 12 o'clock, clockwise)."""
    def pt(a: float) -> tuple[float, float]:
        t = math.radians(a - 90)
        return cx + r * math.cos(t), cy + r * math.sin(t)
    x0, y0 = pt(a0)
    x1, y1 = pt(a1)
    large = 1 if (a1 - a0) % 360 > 180 else 0
    return f"M{x0:.2f} {y0:.2f} A{r:.2f} {r:.2f} 0 {large} 1 {x1:.2f} {y1:.2f}"


def ring(prefix: str, box: list[float], fraction: float, *, color: str = "#2E5BFF", track: str = "#E3EAFF",
         thickness: float = 0.12, start: int = 0, fill_frames: int = 30, span: list[int] | None = None) -> list[dict[str, Any]]:
    """A track circle and a value arc (fraction 0..1) whose stroke draws in over fill_frames."""
    x, y, w, h = box
    s = min(w, h)
    sw = s * thickness
    r = (100 - thickness * 100) / 2
    vb = [100, 100]
    track_d = _arc(50, 50, r, 0, 359.99)
    val_d = _arc(50, 50, r, 0, max(0.01, min(359.99, 360 * fraction)))
    sq = [x + (w - s) / 2, y + (h - s) / 2, s, s]
    base = {"type": "shape", "shape": "path", "viewbox": vb, "box": [round(v, 1) for v in sq]}
    stroke_w = sw * 100 / s
    layers = [{**base, "id": f"{prefix}-track", "role": "decor", "d": track_d,
               "stroke": {"width": stroke_w, "color": track, "cap": "round"}},
              {**base, "id": f"{prefix}-value", "role": "content", "d": val_d,
               "stroke": {"width": stroke_w, "color": color, "cap": "round"},
               "draw": [{"f": start, "v": 0}, {"f": start + fill_frames, "v": 1, "ease": "out"}] if fill_frames else 1,
               "note": f"ring value {fraction:.3f}"}]
    if span:
        for l in layers:
            l["span"] = span
    return layers


def pictogram(prefix: str, box: list[float], rows: int, cols: int, filled: int, *, color: str = "#2E5BFF",
              empty: str = "#D6DEF5", shape: str = "dot", start: int = 0, fill_frames: int = 0,
              span: list[int] | None = None) -> list[dict[str, Any]]:
    """A rows x cols grid; the first `filled` cells take `color`. With fill_frames the filled group fades in."""
    x, y, w, h = box
    cw, ch = w / cols, h / rows
    rad = min(cw, ch) * 0.38

    def cell(i: int) -> str:
        r, c = divmod(i, cols)
        cx, cy = c * cw + cw / 2, r * ch + ch / 2
        if shape == "square":
            return f"M{cx - rad:.1f} {cy - rad:.1f} h{2 * rad:.1f} v{2 * rad:.1f} h{-2 * rad:.1f} Z"
        return (f"M{cx - rad:.1f} {cy:.1f} a{rad:.1f} {rad:.1f} 0 1 0 {2 * rad:.1f} 0 "
                f"a{rad:.1f} {rad:.1f} 0 1 0 {-2 * rad:.1f} 0 Z")

    n = rows * cols
    on = " ".join(cell(i) for i in range(min(filled, n)))
    off = " ".join(cell(i) for i in range(min(filled, n), n))
    base = {"type": "shape", "shape": "path", "viewbox": [round(w, 1), round(h, 1)], "box": [x, y, w, h]}
    layers = []
    if off:
        layers.append({**base, "id": f"{prefix}-empty", "role": "decor", "d": off, "fill": empty})
    if on:
        layers.append({**base, "id": f"{prefix}-filled", "role": "content", "d": on, "fill": color,
                       "opacity": [{"f": start, "v": 0}, {"f": start + fill_frames, "v": 1}] if fill_frames else 1.0,
                       "note": f"{filled} of {n} highlighted"})
    if span:
        for l in layers:
            l["span"] = span
    return layers


def arrow(prefix: str, start_pt: list[float], end_pt: list[float], *, color: str = "#2E5BFF", width: float = 8,
          head: float = 26, start: int = 0, draw_frames: int = 0, span: list[int] | None = None) -> list[dict[str, Any]]:
    (x0, y0), (x1, y1) = start_pt, end_pt
    pad = head + width
    bx, by = min(x0, x1) - pad, min(y0, y1) - pad
    bw, bh = abs(x1 - x0) + 2 * pad, abs(y1 - y0) + 2 * pad
    lx0, ly0, lx1, ly1 = x0 - bx, y0 - by, x1 - bx, y1 - by
    ang = math.atan2(ly1 - ly0, lx1 - lx0)
    hx1 = lx1 - head * math.cos(ang - math.radians(28))
    hy1 = ly1 - head * math.sin(ang - math.radians(28))
    hx2 = lx1 - head * math.cos(ang + math.radians(28))
    hy2 = ly1 - head * math.sin(ang + math.radians(28))
    d = f"M{lx0:.1f} {ly0:.1f} L{lx1:.1f} {ly1:.1f} M{hx1:.1f} {hy1:.1f} L{lx1:.1f} {ly1:.1f} L{hx2:.1f} {hy2:.1f}"
    layer = {"id": prefix, "type": "shape", "shape": "path", "role": "decor", "viewbox": [round(bw, 1), round(bh, 1)],
             "box": [round(bx, 1), round(by, 1), round(bw, 1), round(bh, 1)], "d": d,
             "stroke": {"width": width, "color": color, "cap": "round"},
             "draw": [{"f": start, "v": 0}, {"f": start + draw_frames, "v": 1, "ease": "out"}] if draw_frames else 1}
    if span:
        layer["span"] = span
    return [layer]


KINDS = {"iso_bars": iso_bars, "ring": ring, "pictogram": pictogram, "arrow": arrow}
