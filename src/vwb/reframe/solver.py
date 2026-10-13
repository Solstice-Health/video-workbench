"""Solve the framing of one output: beats -> focus -> camera, swept and scored.

The score is a cost (lower is better) built from measurable things: focus text cut by the
window edge, focus text below the legibility floor, faces cut, text outside the safe zone,
and camera motion. The best plan wins; the runners-up stay as alternatives for the critic.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from typing import Any

from vwb.config import lever
from vwb.index.ocr import iou  # noqa: F401  (re-exported for tools)
from vwb.index.store import IndexStore
from vwb.reframe import camera
from vwb.reframe.focus import FocusFinder, split_beats



@dataclass
class TimeMap:
    """Output frames <- source frames, as concatenated half-open spans."""
    spans: list[tuple[int, int]]

    @property
    def length(self) -> int:
        return sum(b - a for a, b in self.spans)

    def offsets(self) -> list[int]:
        out, acc = [], 0
        for a, b in self.spans:
            out.append(acc)
            acc += b - a
        return out

    def to_source(self, f: int) -> int:
        acc = 0
        for a, b in self.spans:
            if f < acc + (b - a):
                return a + (f - acc)
            acc += b - a
        return self.spans[-1][1] - 1

    def to_output(self, s: int) -> int | None:
        acc = 0
        for a, b in self.spans:
            if a <= s < b:
                return acc + (s - a)
            acc += b - a
        return None


@dataclass
class Target:
    """Where the agent will place the video window, in output pixels."""
    box: tuple[float, float, float, float]          # x, y, w, h
    safe: tuple[float, float, float, float]         # platform safe zone x, y, w, h
    canvas_h: float

    @property
    def aspect(self) -> float:
        return self.box[2] / self.box[3]


@dataclass
class Solution:
    box: list[float]
    params: dict[str, Any]
    keyframes: list[dict[str, Any]]
    beats: list[dict[str, Any]]
    metrics: dict[str, Any]
    cost: float
    risks: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_beats(store: IndexStore, tm: TimeMap, finder: FocusFinder) -> list[dict[str, Any]]:
    fps = float(store.media()["fps"])
    shots = store.all("SELECT * FROM shots ORDER BY id")

    def shot_at(sf: int) -> int:
        for s in shots:
            if s["f0"] <= sf < s["f1"]:
                return s["id"]
        return shots[-1]["id"]

    beats: list[dict[str, Any]] = []
    for (a, b), off in zip(tm.spans, tm.offsets()):
        words = store.all("SELECT * FROM words WHERE f0 >= ? AND f1 <= ? ORDER BY t0", (a, b + 1))
        groups = split_beats(words) if words else []
        raw = []
        for g in groups:
            raw.append({"s0": g[0]["f0"], "s1": g[-1]["f1"], "text": " ".join(w["text"] for w in g)})
        if not raw:
            raw = [{"s0": a, "s1": b, "text": ""}]
        # cover the span: boundaries at the middle of the gaps between beats
        for i, r in enumerate(raw):
            lo = a if i == 0 else (raw[i - 1]["s1"] + r["s0"]) // 2
            hi = b if i == len(raw) - 1 else (r["s1"] + raw[i + 1]["s0"]) // 2
            r["c0"], r["c1"] = max(a, lo), min(b, max(hi, lo + 1))
        for r in raw:
            # split a beat at any source cut inside it
            cuts = [s["f0"] for s in shots if r["c0"] < s["f0"] < r["c1"]]
            edges = [r["c0"], *cuts, r["c1"]]
            for e0, e1 in zip(edges, edges[1:]):
                if e1 - e0 < 1:
                    continue
                shot = shot_at(e0)
                f = finder.focus(e0, e1, shot, r["text"])
                beats.append({"o0": off + (e0 - a), "o1": off + (e1 - a), "s0": e0, "s1": e1, "shot": shot,
                              "text": r["text"], "focus_box": f.box, "focus_kind": f.kind, "focus_reason": f.reason,
                              "focus_tracks": f.tracks, "focus_cap_h": f.min_cap_h,
                              "obstacles": finder.obstacles(e0, e1, shot, set(f.tracks))})
    beats.sort(key=lambda x: x["o0"])
    # continuity: a beat with no text match keeps the framing of a matched neighbour in its shot
    for i, b in enumerate(beats):
        if b["focus_kind"] not in ("content", "frame", "saliency"):
            continue
        nb = next((beats[j] for j in (i - 1, i + 1) if 0 <= j < len(beats) and beats[j]["shot"] == b["shot"]
                   and beats[j]["focus_kind"] == "text"), None)
        if nb is not None:
            b.update(focus_box=nb["focus_box"], focus_kind="text-inherited", focus_tracks=list(nb["focus_tracks"]),
                     focus_cap_h=nb["focus_cap_h"], focus_reason=f"no text match; keeps the framing of the next idea "
                     f"in this shot ({nb['focus_reason'][:40]})", obstacles=nb["obstacles"])
    return beats


def evaluate(store: IndexStore, tm: TimeMap, target: Target, keys: list[camera.Keyframe], beats: list[dict[str, Any]],
             legibility_floor: float, step: int = 3) -> tuple[float, dict[str, Any], list[dict[str, Any]]]:
    m = store.media()
    fps = float(m["fps"])
    tracks = store.all("SELECT * FROM text_tracks")
    faces = store.all("SELECT * FROM faces ORDER BY f")
    cx_, cy_, cw_, ch_ = target.box
    sx_, sy_, sw_, sh_ = target.safe
    ui_band_top = target.canvas_h * 0.52
    keyd = [k.to_dict() for k in keys]
    focus_by_frame = {}
    for b in beats:
        for f in range(b["o0"], b["o1"]):
            focus_by_frame[f] = b
    clipped: dict[int, int] = {}
    seen: dict[int, int] = {}
    visible: dict[int, int] = {}
    legib: list[float] = []
    face_cut = face_total = 0
    safe_viol = 0
    motion = 0.0
    prev = None
    max_scale = 0.0
    n = 0
    for f in range(0, tm.length, step):
        n += 1
        win = camera.rect_at(keyd, f)
        wx, wy, ww, wh = win
        scale = cw_ / ww
        max_scale = max(max_scale, scale)
        if prev is not None:
            px, py, pw, ph = prev
            motion += (abs(wx + ww / 2 - px - pw / 2) + abs(wy + wh / 2 - py - ph / 2)) / ww + abs(ww - pw) / ww
        prev = win
        sf = tm.to_source(f)
        beat = focus_by_frame.get(f)
        focus_ids = set(beat["focus_tracks"]) if beat else set()
        for t in tracks:
            if not (t["f0"] <= sf < t["f1"]) or t["role"] == "logo_text":
                continue
            seen[t["id"]] = seen.get(t["id"], 0) + 1
            ix = max(0.0, min(t["x"] + t["w"], wx + ww) - max(t["x"], wx))
            iy = max(0.0, min(t["y"] + t["h"], wy + wh) - max(t["y"], wy))
            vis = (ix * iy) / max(1.0, t["w"] * t["h"])
            if vis > 0.02:
                visible[t["id"]] = visible.get(t["id"], 0) + 1
            if 0.02 < vis < 0.97:
                clipped[t["id"]] = clipped.get(t["id"], 0) + 1
            if vis >= 0.97:
                ox0 = cx_ + (t["x"] - wx) * scale
                ox1 = cx_ + (t["x"] + t["w"] - wx) * scale
                oyc = cy_ + (t["y"] + t["h"] / 2 - wy) * scale
                # Shorts UI: action buttons run down the right edge in the lower half of the screen
                if t["id"] in focus_ids and (ox0 < sx_ - 1 or (ox1 > sx_ + sw_ + 1 and oyc > ui_band_top)):
                    safe_viol += 1
                if t["id"] in focus_ids and t["cap_h"]:
                    legib.append(t["cap_h"] * scale)
        for fc in faces:
            if abs(fc["f"] - sf) <= 3:
                face_total += 1
                ix = max(0.0, min(fc["x"] + fc["w"], wx + ww) - max(fc["x"], wx))
                iy = max(0.0, min(fc["y"] + fc["h"], wy + wh) - max(fc["y"], wy))
                inside = (ix * iy) / max(1.0, fc["w"] * fc["h"])
                if 0.0 < inside < 0.7:
                    face_cut += 1
    partial_ratio = sum(clipped.values()) / max(1, sum(visible.values()))
    focus_tracks = {t for b in beats for t in b["focus_tracks"]}
    focus_clip = sum(clipped.get(t, 0) for t in focus_tracks) / max(1, sum(seen.get(t, 0) for t in focus_tracks))
    legib_deficit = sum(max(0.0, legibility_floor - c) / legibility_floor for c in legib) / max(1, len(legib))
    dur_s = tm.length / fps
    metrics = {
        "frames_checked": n, "max_scale": round(max_scale, 3), "motion_per_s": round(motion / max(dur_s, 1e-6), 4),
        "focus_clip_ratio": round(focus_clip, 4), "legibility_min_px": round(min(legib), 1) if legib else None,
        "legibility_deficit": round(legib_deficit, 4), "face_cut_ratio": round(face_cut / max(1, face_total), 4),
        "safe_violations": safe_viol, "n_beats": len(beats), "partial_text_ratio": round(partial_ratio, 4),
        "beats_by_focus": {k: sum(1 for b in beats if b["focus_kind"] == k) for k in {b["focus_kind"] for b in beats}},
    }
    cost = (3.0 * focus_clip + 2.5 * legib_deficit + 0.6 * metrics["motion_per_s"]
            + 3.0 * metrics["face_cut_ratio"] + 0.02 * safe_viol + 1.0 * partial_ratio)
    risks = []
    by_id = {t["id"]: t for t in tracks}
    for tid, count in clipped.items():
        t = by_id[tid]
        ratio = count / max(1, seen.get(tid, 1))
        if ratio > 0.25:
            risks.append({"kind": "TEXT-CLIPPED", "track": tid, "role": t["role"], "regulated": bool(t["regulated"]),
                          "ratio": round(ratio, 2), "text": t["text"][:120]})
    for tid, count in seen.items():
        t = by_id[tid]
        if visible.get(tid, 0) == 0 and count * step >= fps * 0.5:
            risks.append({"kind": "TEXT-DROPPED", "track": tid, "role": t["role"], "regulated": bool(t["regulated"]),
                          "text": t["text"][:120]})
    if legib and min(legib) < legibility_floor:
        risks.append({"kind": "LEGIBILITY-BELOW-FLOOR", "min_px": round(min(legib), 1), "floor": legibility_floor})
    if metrics["face_cut_ratio"] > 0.05:
        risks.append({"kind": "FACE-CUT", "ratio": metrics["face_cut_ratio"]})
    if max_scale > 1.25:
        risks.append({"kind": "UPSCALED", "max_scale": round(max_scale, 2)})
    return cost, metrics, risks


def solve(store: IndexStore, spans: list[tuple[int, int]], target: Target, legibility_floor: float = 26.0,
          k: int | None = None, focus: str = "auto") -> Solution:
    """Crop keyframes for one video window at target.box. focus='auto' follows the narration;
    'presenter' keeps the speaker; 'content' frames the shot's main content block."""
    m = store.media()
    W, H, fps = float(m["width"]), float(m["height"]), float(m["fps"])
    tm = TimeMap([(int(a), int(b)) for a, b in spans])
    finder = FocusFinder(store)
    beats = build_beats(store, tm, finder)
    if focus in ("presenter", "content"):
        for b in beats:
            box = finder.presenter_box(b["s0"], b["s1"]) if focus == "presenter" else finder.main_content(b["shot"])
            if box:
                b.update(focus_box=box, focus_kind=focus, focus_reason=f"forced: {focus}", focus_tracks=[])
    k = k or int(lever("reframe.sweeps").get("k", 12))
    grid = list(itertools.product([0.03, 0.06, 0.10], [2.0, 2.4, 2.8], [0.6, 0.85], [0.0, 0.03]))
    stride = max(1, len(grid) // k)
    grid = grid[::stride][:k] + [(0.06, 2.4, 0.7, 0.03), (0.03, 2.4, 0.7, 0.0)]
    results = []
    for margin, max_scale, move_s, drift in grid:
        p = camera.CameraParams(aspect=target.aspect, out_w=target.box[2], margin=margin, max_scale=max_scale,
                                move_s=move_s, drift=drift, safe_left=0.03, safe_right=0.97)
        keys = camera.plan(beats, W, H, fps, p)
        cost, metrics, risks = evaluate(store, tm, target, keys, beats, legibility_floor)
        results.append((cost, {"margin": margin, "max_scale": max_scale, "move_s": move_s, "drift": drift},
                        keys, metrics, risks))
    results.sort(key=lambda r: r[0])
    best = results[0]
    finalists = int(lever("reframe.finalists").get("k", 4))
    alts = [{"cost": round(r[0], 4), "params": r[1], "metrics": r[3], "keyframes": [kf.to_dict() for kf in r[2]]}
            for r in results[1:finalists]]
    clean_beats = [{kk: (round(v, 1) if isinstance(v, float) else v) for kk, v in b.items()
                    if kk not in ("focus_box", "obstacles")}
                   | {"focus_box": [round(v, 1) for v in b["focus_box"]]} for b in beats]
    return Solution(box=list(target.box), params=best[1], keyframes=[kf.to_dict() for kf in best[2]],
                    beats=clean_beats, metrics=best[3], cost=round(best[0], 4), risks=best[4], alternatives=alts)
