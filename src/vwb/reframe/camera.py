"""The virtual camera: one window per beat, held still, with eased moves between beats.

A human editor reframing a slide video holds a shot while a point is made, then moves with
purpose to the next point. So: hold during a beat, move in the pause before the next one,
cut (not pan) across a source cut, and skip moves that would barely change the picture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EASES = ("hold", "linear", "inout")


@dataclass
class Keyframe:
    f: int                                     # output frame
    rect: tuple[float, float, float, float]    # source window x, y, w, h
    ease: str = "inout"                        # interpolation from the previous keyframe

    def to_dict(self) -> dict[str, Any]:
        """The canvas keyframe format (vwb.canvas/2)."""
        return {"f": self.f, "v": [round(v, 2) for v in self.rect], "ease": self.ease}


@dataclass
class CameraParams:
    aspect: float                  # window aspect = content rect aspect
    out_w: float                   # content rect width in output px
    margin: float = 0.10           # breathing room around the focus, per side
    max_scale: float = 2.4         # most zoom (output px per source px)
    safe_left: float = 0.0556      # fraction of the content width where text may start (safe zone)
    safe_right: float = 0.8889     # fraction where text must end
    move_s: float = 0.7            # duration of a move
    lead_s: float = 0.25           # a move ends this long before the next beat's first word
    min_change: float = 0.18       # skip moves smaller than this (relative)
    drift: float = 0.03            # slow push-in over long holds (0 disables)


def fit_window(focus: tuple[float, float, float, float], src_w: float, src_h: float, p: CameraParams) -> tuple[float, float, float, float]:
    fx, fy, fw, fh = focus
    fw_m, fh_m = fw * (1 + 2 * p.margin), fh * (1 + 2 * p.margin)
    safe_span = p.safe_right - p.safe_left
    w = max(fw_m / safe_span, fh_m * p.aspect)
    min_w = p.out_w / p.max_scale
    max_w = min(src_w, src_h * p.aspect)
    w = min(max(w, min_w), max_w)
    h = w / p.aspect
    # place the focus centre at the centre of the safe span, not of the window
    safe_mid = (p.safe_left + p.safe_right) / 2
    x = fx + fw / 2 - safe_mid * w
    y = fy + fh / 2 - h / 2
    x = min(max(x, 0.0), src_w - w)
    y = min(max(y, 0.0), src_h - h)
    return (x, y, w, h)


def _frac_inside(win: tuple[float, ...], box: tuple[float, ...]) -> float:
    wx, wy, ww, wh = win
    bx, by, bw, bh = box
    ix = max(0.0, min(bx + bw, wx + ww) - max(bx, wx))
    iy = max(0.0, min(by + bh, wy + wh) - max(by, wy))
    return (ix * iy) / max(1.0, bw * bh)


def _clamp(win: tuple[float, float, float, float], aspect: float, src_w: float, src_h: float,
           min_w: float, max_w: float) -> tuple[float, float, float, float]:
    x, y, w, h = win
    w = min(max(w, min_w), max_w)
    h = w / aspect
    cx, cy = x + win[2] / 2, y + win[3] / 2
    x, y = cx - w / 2, cy - h / 2
    return (min(max(x, 0.0), src_w - w), min(max(y, 0.0), src_h - h), w, h)


def clean_window(win: tuple[float, float, float, float], focus: tuple[float, float, float, float],
                 obstacles: list[tuple[float, float, float, float]], p: "CameraParams", src_w: float, src_h: float,
                 rounds: int = 3) -> tuple[float, float, float, float]:
    """Move or resize the window so no text line or face is cut in half by its edge.

    Candidates: the window itself, zoomed in or out about the focus, shifted just past each
    cut object, or grown to include it. The focus must stay inside. Fewest cut objects wins,
    then the smallest change.
    """
    import math

    min_w = p.out_w / p.max_scale
    max_w = min(src_w, src_h * p.aspect)
    fcx, fcy = focus[0] + focus[2] / 2, focus[1] + focus[3] / 2
    pad = 6.0

    def cut(w: tuple[float, ...]) -> list[tuple[float, ...]]:
        return [b for b in obstacles if 0.04 < _frac_inside(w, b) < 0.96]

    def score(w: tuple[float, ...]) -> float:
        f = _frac_inside(w, focus)
        pen = sum(1.0 + min(1.0, 4 * b[2] * b[3] / (w[2] * w[3])) for b in cut(w))
        change = abs(math.log(w[2] / win[2])) + (abs(w[0] + w[2] / 2 - win[0] - win[2] / 2)
                                                  + abs(w[1] + w[3] / 2 - win[1] - win[3] / 2)) / win[2]
        return (0.0 if f > 0.985 else 60.0 * (1 - f)) + 6.0 * pen + 2.0 * change

    best = win
    for _ in range(rounds):
        cands = [best]
        for k in (0.8, 0.88, 0.94, 1.06, 1.14, 1.25, 1.4):
            w2, h2 = best[2] * k, best[3] * k
            cands.append(_clamp((fcx - w2 / 2, fcy - h2 / 2, w2, h2), p.aspect, src_w, src_h, min_w, max_w))
        for b in cut(best):
            bx, by, bw, bh = b
            x, y, w, h = best
            shifts = []
            if bx < x < bx + bw:
                shifts += [(bx + bw - x + pad, 0.0), (bx - x - pad, 0.0)]
            if bx < x + w < bx + bw:
                shifts += [(bx - (x + w) - pad, 0.0), (bx + bw - (x + w) + pad, 0.0)]
            if by < y < by + bh:
                shifts += [(0.0, by + bh - y + pad), (0.0, by - y - pad)]
            if by < y + h < by + bh:
                shifts += [(0.0, by - (y + h) - pad), (0.0, by + bh - (y + h) + pad)]
            for dx, dy in shifts:
                cands.append(_clamp((x + dx, y + dy, w, h), p.aspect, src_w, src_h, min_w, max_w))
            ux0, uy0 = min(x, bx), min(y, by)
            ux1, uy1 = max(x + w, bx + bw), max(y + h, by + bh)
            uw = max(ux1 - ux0, (uy1 - uy0) * p.aspect) + 2 * pad
            cands.append(_clamp(((ux0 + ux1) / 2 - uw / 2, (uy0 + uy1) / 2 - uw / p.aspect / 2, uw, uw / p.aspect),
                                p.aspect, src_w, src_h, min_w, max_w))
        nxt = min(cands, key=score)
        if nxt == best:
            break
        best = nxt
    return best


def best_window(focus: tuple[float, float, float, float], obstacles: list[tuple[float, float, float, float]],
                src_w: float, src_h: float, p: "CameraParams") -> tuple[float, float, float, float]:
    """Exhaustive search over windows that contain the focus (with margin), vectorised.

    A window is good when no text line or face is cut in half by its edge, when it is tight
    (bigger type), and when the focus sits in the middle of the safe span. Every size from
    the tightest fit to the widest allowed, and every position that still holds the focus,
    is scored; the best one wins. Ties never matter: the grid is deterministic.
    """
    import numpy as np

    fx, fy, fw, fh = focus
    safe_span = p.safe_right - p.safe_left
    min_w = p.out_w / p.max_scale
    max_w = min(src_w, src_h * p.aspect)
    fit_w = max(fw * (1 + 2 * p.margin) / safe_span, fh * (1 + 2 * p.margin) * p.aspect, min_w)
    fit_w = min(fit_w, max_w)
    widths = np.unique(np.clip(fit_w * (1.07 ** np.arange(0, 14)), fit_w, max_w))
    cand = []
    for w in widths:
        h = w / p.aspect
        # x range: the focus stays inside the safe span; y range: the focus stays inside the window
        x_lo = max(0.0, fx + fw - p.safe_right * w)
        x_hi = min(src_w - w, fx - p.safe_left * w)
        y_lo = max(0.0, fy + fh - h)
        y_hi = min(src_h - h, fy)
        if x_hi < x_lo:
            x_lo = x_hi = min(max(fx + fw / 2 - (p.safe_left + safe_span / 2) * w, 0.0), src_w - w)
        if y_hi < y_lo:
            y_lo = y_hi = min(max(fy + fh / 2 - h / 2, 0.0), src_h - h)
        xs = np.linspace(x_lo, x_hi, 17) if x_hi > x_lo else np.array([x_lo])
        ys = np.linspace(y_lo, y_hi, 17) if y_hi > y_lo else np.array([y_lo])
        gx, gy = np.meshgrid(xs, ys)
        cand.append(np.stack([gx.ravel(), gy.ravel(), np.full(gx.size, w), np.full(gx.size, h)], axis=1))
    C = np.concatenate(cand)
    if obstacles:
        O = np.array(obstacles, dtype=float)
        ix = np.clip(np.minimum(C[:, None, 0] + C[:, None, 2], O[None, :, 0] + O[None, :, 2])
                     - np.maximum(C[:, None, 0], O[None, :, 0]), 0, None)
        iy = np.clip(np.minimum(C[:, None, 1] + C[:, None, 3], O[None, :, 1] + O[None, :, 3])
                     - np.maximum(C[:, None, 1], O[None, :, 1]), 0, None)
        vis = ix * iy / np.maximum(O[None, :, 2] * O[None, :, 3], 1.0)
        partial = ((vis > 0.04) & (vis < 0.96)).sum(axis=1)
    else:
        partial = np.zeros(len(C))
    cx = C[:, 0] + (p.safe_left + safe_span / 2) * C[:, 2]
    cy = C[:, 1] + C[:, 3] / 2
    offc = np.abs(cx - (fx + fw / 2)) / C[:, 2] + np.abs(cy - (fy + fh / 2)) / C[:, 3]
    score = 10.0 * partial + 1.2 * (C[:, 2] / fit_w - 1.0) + 0.8 * offc
    best = C[int(np.argmin(score))]
    return (float(best[0]), float(best[1]), float(best[2]), float(best[3]))


def rel_change(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dc = ((ax + aw / 2 - bx - bw / 2) ** 2 + (ay + ah / 2 - by - bh / 2) ** 2) ** 0.5 / max(aw, bw)
    dz = abs(aw - bw) / max(aw, bw)
    return dc + dz


def shrink(rect: tuple[float, float, float, float], k: float, src_w: float, src_h: float) -> tuple[float, float, float, float]:
    x, y, w, h = rect
    nw, nh = w * (1 - k), h * (1 - k)
    nx, ny = x + (w - nw) / 2, y + (h - nh) / 2
    return (min(max(nx, 0), src_w - nw), min(max(ny, 0), src_h - nh), nw, nh)


def plan(beats: list[dict[str, Any]], src_w: float, src_h: float, fps: float, p: CameraParams) -> list[Keyframe]:
    """beats: [{o0, o1, focus_box, shot}] in output frames, in order, covering the timeline."""
    if not beats:
        return []
    windows = [best_window(b["focus_box"], b.get("obstacles", []), src_w, src_h, p) for b in beats]
    # merge near-identical neighbours so the camera does not twitch
    for i in range(1, len(windows)):
        if beats[i]["shot"] == beats[i - 1]["shot"] and rel_change(windows[i], windows[i - 1]) < p.min_change:
            windows[i] = windows[i - 1]
    keys: list[Keyframe] = [Keyframe(0, windows[0], "hold")]
    move = max(2, int(round(p.move_s * fps)))
    lead = int(round(p.lead_s * fps))
    for i in range(1, len(beats)):
        prev_w, cur_w = windows[i - 1], windows[i]
        start = beats[i]["o0"]
        if beats[i]["shot"] != beats[i - 1]["shot"]:
            # a source cut: cut the camera too, exactly on the cut frame ("hold" jumps at the key)
            keys.append(Keyframe(start, cur_w, "hold"))
            continue
        if cur_w == prev_w:
            continue
        end_f = max(beats[i - 1]["o0"] + move, start - lead)
        begin_f = max(keys[-1].f + 1, end_f - move)
        hold_rect = prev_w
        if p.drift and beats[i - 1]["o1"] - beats[i - 1]["o0"] > 4 * fps and _drift_ok(prev_w, beats[i - 1], p, src_w, src_h):
            hold_rect = shrink(prev_w, p.drift, src_w, src_h)
            keys.append(Keyframe(begin_f, hold_rect, "linear"))
        else:
            keys.append(Keyframe(begin_f, hold_rect, "hold"))
        keys.append(Keyframe(max(end_f, begin_f + 2), cur_w, "inout"))
    last = beats[-1]
    if p.drift and last["o1"] - (keys[-1].f) > 4 * fps and _drift_ok(keys[-1].rect, last, p, src_w, src_h):
        keys.append(Keyframe(last["o1"] - 1, shrink(keys[-1].rect, p.drift, src_w, src_h), "linear"))
    # keyframes must be strictly increasing in time
    clean: list[Keyframe] = []
    for k in keys:
        if clean and k.f <= clean[-1].f:
            clean[-1] = Keyframe(clean[-1].f, k.rect, k.ease)
        else:
            clean.append(k)
    return clean


def _drift_ok(rect: tuple[float, float, float, float], beat: dict[str, Any], p: CameraParams,
              src_w: float, src_h: float) -> bool:
    """A slow push-in is allowed only if it cuts nothing the held window showed whole."""
    after = shrink(rect, p.drift, src_w, src_h)
    for b in beat.get("obstacles", []) + [beat["focus_box"]]:
        if _frac_inside(rect, b) >= 0.96 and _frac_inside(after, b) < 0.96:
            return False
    return True


def rect_at(keys: list[dict[str, Any]] | list[Keyframe], f: int) -> tuple[float, float, float, float]:
    """The window at output frame f (same interpolation as the compositor)."""
    from vwb.canvas.anim import value_at
    ks = [k if isinstance(k, dict) else k.to_dict() for k in keys]
    return tuple(value_at(ks, f))  # type: ignore[return-value]
