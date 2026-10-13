"""On-screen text: change-driven OCR sampling, text tracks, a second reader and roles.

Reader 1 (Apple Vision) reads sampled frames: whenever the picture changes, and at least
once a second. Lines that persist across samples merge into a text track with a time span
and a box. Reader 2 (RapidOCR, PP-OCR models) reads each track's crop once. A track is
"agree" only when both readers return the same normalised string.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from typing import Any

import numpy as np

# ---------------------------------------------------------------- roles

USAGE_TERMS = re.compile(
    r"(intended (only )?for|for (use by )?(us )?(health ?care|medical) professionals|healthcare professionals only|"
    r"viewed in (its|their) entirety|educational purposes|not intended to replace|for internal use|"
    r"not for (promotional|distribution)|do not (copy|distribute))", re.I)
JOB_CODE = re.compile(r"\b(?:[A-Z]{2,5}-){2,}[A-Z0-9]{2,}\b|\b[A-Z]{2,4}-[A-Z]{2,4}-\d{3,}\b|\bPP-[A-Z0-9-]+\b")
LEGAL = re.compile(r"(©|\(c\)|all rights reserved|trademark|®|™)", re.I)
REFERENCE = re.compile(r"(\bet al\b|\bdoi\b|\bAccessed\b|\bJ\s?[A-Z][a-z]+|\bMed\b|\bLancet\b|\bVaccine\b.*\d{4}|"
                       r"\bReferences?\b|^\s*\d+\.\s+[A-Z][a-z]+ [A-Z]{1,3}\b|\bpublished\b|\bMMWR\b|\b\d{4};\d+)", re.I)
FOOTNOTE_LEAD = re.compile(r"^\s*([*†‡§¶#]|[a-z]\s|\d+\s?[A-Z])")
CLAIM_HINT = re.compile(r"(\d+(\.\d+)?\s?%|\d+(\.\d+)?-fold|\bfold\b|\bincreased?\b|\bhigher\b|\blower\b|\brisk\b|"
                        r"\breduc|\bmost common\b|\bmillion\b|\bx\b\s*\d|\b\d+\s?(times|in \d+)\b)", re.I)
ISI = re.compile(r"important safety information|indications? and usage|contraindications|warnings and precautions|"
                 r"adverse reactions", re.I)

REGULATED_ROLES = {"claim", "footnote", "reference", "job_code", "legal", "usage_terms", "isi"}


def norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def iou(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    x0, y0 = max(ax0, bx0), max(ay0, by0)
    x1, y1 = min(ax0 + aw, bx0 + bw), min(ay0 + ah, by0 + bh)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def is_rotated(track: dict[str, Any]) -> bool:
    """Vertical text (axis titles): the box is much taller than wide for a multi-letter string."""
    return len(track["text"]) >= 4 and track["h"] > 1.6 * track["w"]


def classify(track: dict[str, Any], frame_w: int, frame_h: int, median_h: float) -> tuple[str, bool]:
    """Role from content patterns first, then position and size."""
    text = track["text"]
    x, y, w, h = track["x"], track["y"], track["w"], track["h"]
    rel_y = (y + h / 2) / frame_h
    if is_rotated(track):
        return "label", False
    if USAGE_TERMS.search(text):
        role = "usage_terms"
    elif ISI.search(text):
        role = "isi"
    elif JOB_CODE.search(text) and len(text) < 60:
        role = "job_code"
    elif LEGAL.search(text) and len(text) < 120:
        role = "legal"
    elif h < 0.72 * median_h and rel_y > 0.78 and (REFERENCE.search(text) or FOOTNOTE_LEAD.match(text) or len(text) > 40):
        role = "reference" if REFERENCE.search(text) else "footnote"
    elif (rel_y < 0.24 and h >= 1.25 * median_h) or h >= 1.9 * median_h:
        role = "title"
    elif CLAIM_HINT.search(text):
        role = "claim"
    elif h < 0.8 * median_h and len(text) < 30:
        role = "label"
    else:
        role = "body"
    return role, role in REGULATED_ROLES


# ---------------------------------------------------------------- sampling

def change_driven_indices(thumbs: list[tuple[int, np.ndarray]], fps: float, max_gap_s: float = 1.0,
                          diff_threshold: float = 1.6) -> list[int]:
    """Pick frames to OCR: the first frame, every visible change, and at least one per max_gap_s."""
    chosen: list[int] = []
    last_thumb = None
    last_idx = -10**9
    for idx, th in thumbs:
        if last_thumb is None:
            chosen.append(idx)
            last_thumb, last_idx = th, idx
            continue
        diff = float(np.mean(np.abs(th.astype(np.int16) - last_thumb.astype(np.int16))))
        if diff > diff_threshold or (idx - last_idx) >= max_gap_s * fps:
            chosen.append(idx)
            last_thumb, last_idx = th, idx
    return chosen


# ---------------------------------------------------------------- tracks

def build_tracks(samples: list[tuple[int, list[dict[str, Any]]]], step_hint: int, n_frames: int,
                 shot_bounds: list[int]) -> list[dict[str, Any]]:
    """Merge per-sample lines into tracks. A line joins a live track when text and box match."""
    live: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    prev_idx = None
    for idx, lines in samples:
        still_live = []
        used = set()
        for tr in live:
            best, best_score = None, 0.0
            for k, ln in enumerate(lines):
                if k in used:
                    continue
                s = similar(tr["votes"].most_common(1)[0][0], ln["text"])
                o = iou(tr["boxes"][-1], ln["box"])
                if s >= 0.82 and o >= 0.45 and s + o > best_score:
                    best, best_score = k, s + o
            if best is not None:
                used.add(best)
                ln = lines[best]
                tr["votes"][ln["text"]] += 1
                tr["boxes"].append(ln["box"])
                tr["confs"].append(ln["conf"])
                tr["last"] = idx
                tr["frames"].append(idx)
                still_live.append(tr)
            else:
                tr["end_hint"] = idx  # first sample where the track was absent
                done.append(tr)
        for k, ln in enumerate(lines):
            if k not in used:
                still_live.append({"first": idx, "last": idx, "frames": [idx], "votes": Counter({ln["text"]: 1}),
                                   "boxes": [ln["box"]], "confs": [ln["conf"]], "start_hint": prev_idx, "end_hint": None})
        live = still_live
        prev_idx = idx
    for tr in live:
        tr["end_hint"] = n_frames
        done.append(tr)

    tracks = []
    for tr in done:
        boxes = np.array(tr["boxes"], dtype=float)
        box = np.median(boxes, axis=0)
        f0, f1 = tr["first"], (tr["end_hint"] if tr["end_hint"] is not None else tr["last"] + step_hint)
        # tracks usually begin and end on a cut: snap to a shot boundary inside the uncertainty window
        lo = tr["start_hint"] if tr["start_hint"] is not None else 0
        f0 = next((b for b in shot_bounds if lo < b <= f0), f0) if lo < f0 else f0
        f1 = next((b for b in shot_bounds if tr["last"] < b <= f1), f1)
        text, count = tr["votes"].most_common(1)[0]
        tracks.append({"f0": int(f0), "f1": int(max(f1, f0 + 1)), "x": float(box[0]), "y": float(box[1]),
                       "w": float(box[2]), "h": float(box[3]), "text": norm_text(text),
                       "conf": float(np.mean(tr["confs"])), "n_seen": len(tr["frames"]),
                       "stability": count / max(1, sum(tr["votes"].values())),
                       "mid_frame": int(tr["frames"][len(tr["frames"]) // 2]), "seen": list(tr["frames"])})
    tracks.sort(key=lambda t: (t["f0"], t["y"], t["x"]))
    return tracks


# ---------------------------------------------------------------- reader 2

class Reader2:
    """RapidOCR (PP-OCR models, Apache-2.0) on full frames, matched to tracks by geometry.

    Full frames work better than crops: the detector needs context, and crops pull in parts
    of neighbouring lines. One frame usually covers every track alive on a slide.
    """

    def __init__(self) -> None:
        from rapidocr import RapidOCR
        self.engine = RapidOCR()

    def read_frame(self, rgb: np.ndarray) -> list[dict[str, Any]]:
        res = self.engine(np.ascontiguousarray(rgb[:, :, ::-1]))  # RapidOCR expects BGR
        if res is None or not getattr(res, "txts", None):
            return []
        lines = []
        for quad, text in zip(res.boxes, res.txts):
            xs, ys = quad[:, 0], quad[:, 1]
            lines.append({"text": norm_text(text), "box": (float(xs.min()), float(ys.min()),
                                                           float(xs.max() - xs.min()), float(ys.max() - ys.min()))})
        return lines


def match_reader2(track_box: tuple[float, float, float, float], lines: list[dict[str, Any]]) -> str:
    """Reader 2's text for one track: lines whose centre sits inside the track box (with slack)."""
    x, y, w, h = track_box
    picked = []
    for ln in lines:
        lx, ly, lw, lh = ln["box"]
        overlap_y = max(0.0, min(ly + lh, y + h) - max(ly, y))
        overlap_x = max(0.0, min(lx + lw, x + w) - max(lx, x))
        if overlap_y >= 0.55 * min(lh, h) and overlap_x >= 0.5 * lw:
            picked.append(ln)
    picked.sort(key=lambda ln: ln["box"][0])
    return norm_text(" ".join(ln["text"] for ln in picked))


def cover_frames(tracks: list[dict[str, Any]]) -> dict[int, int]:
    """Pick few frames so that every track is read once: track index -> frame."""
    chosen: list[int] = []
    assign: dict[int, int] = {}
    for i in sorted(range(len(tracks)), key=lambda k: -len(tracks[k]["seen"])):
        seen = set(tracks[i]["seen"])
        hit = next((f for f in chosen if f in seen), None)
        if hit is None:
            hit = tracks[i]["mid_frame"]
            chosen.append(hit)
        assign[i] = hit
    return assign


_GLYPH_FOLD = str.maketrans({"–": "-", "—": "-", "‐": "-", "‑": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "..."})


def agree_key(text: str) -> str:
    """Readers cannot tell a hyphen from an en dash or see a bullet glyph; fold those only."""
    t = norm_text(text).translate(_GLYPH_FOLD)
    t = re.sub(r"^[•·▪■◦●\-*]\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()


def readers_agree(a: str, b: str) -> bool:
    """Exact after Unicode, whitespace and dash/quote/bullet folding (UNCALIBRATED rule)."""
    return bool(a) and agree_key(a) == agree_key(b)
