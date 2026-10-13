"""Regulated-content scan. Rules over on-screen text and speech come first (facts);
model readings add hazards as claims. The Clip and Resize recipes read this table."""

from __future__ import annotations

import re
from typing import Any

from vwb.index.ocr import CLAIM_HINT, USAGE_TERMS

SPOKEN_CLAIM = re.compile(r"(\d+(\.\d+)?\s?(%|percent)|\b\d+(\.\d+)?-fold\b|\b(times|fold) (higher|greater|more)\b|"
                          r"\bincreased? risk\b|\bhigher risk\b|\bmost common\b|\bmillion\b|\breduc(e|es|ed|tion)\b)", re.I)
SPOKEN_USAGE = re.compile(r"(intended (only )?for|healthcare professionals|in its entirety|educational purposes)", re.I)


def scan(tracks: list[dict[str, Any]], sents: list[dict[str, Any]], fps: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # on-screen: merge usage-terms lines of one shot into one statement
    by_shot: dict[int, list[dict[str, Any]]] = {}
    for t in tracks:
        if t["role"] == "usage_terms" or USAGE_TERMS.search(t["text"]):
            by_shot.setdefault(t.get("shot", -1), []).append(t)
    for shot, ts in by_shot.items():
        ts.sort(key=lambda t: (t["y"], t["x"]))
        x0 = min(t["x"] for t in ts); y0 = min(t["y"] for t in ts)
        x1 = max(t["x"] + t["w"] for t in ts); y1 = max(t["y"] + t["h"] for t in ts)
        texts: list[str] = []
        for t in sorted(ts, key=lambda t: (-len(t["text"]), t["y"])):
            if not any(t["text"] in other for other in texts):
                texts.append(t["text"])
        ordered = sorted(texts, key=lambda txt: min(t["y"] for t in ts if t["text"] == txt))
        rows.append(_row("usage_terms", min(t["f0"] for t in ts), max(t["f1"] for t in ts), fps,
                         " ".join(ordered), (x0, y0, x1 - x0, y1 - y0), "rules.ocr", "fact",
                         {"tracks": [t["id"] for t in ts], "shot": shot}))
    for t in tracks:
        if t["role"] in ("job_code", "isi"):
            rows.append(_row(t["role"], t["f0"], t["f1"], fps, t["text"], (t["x"], t["y"], t["w"], t["h"]),
                             "rules.ocr", "fact", {"track": t["id"]}))
        elif t["role"] == "claim" and CLAIM_HINT.search(t["text"]):
            rows.append(_row("claim", t["f0"], t["f1"], fps, t["text"], (t["x"], t["y"], t["w"], t["h"]),
                             "rules.ocr", "fact", {"track": t["id"]}))
        elif t["role"] in ("reference", "footnote"):
            rows.append(_row("reference", t["f0"], t["f1"], fps, t["text"], (t["x"], t["y"], t["w"], t["h"]),
                             "rules.ocr", "fact", {"track": t["id"], "role": t["role"]}))
    for s in sents:
        if SPOKEN_USAGE.search(s["text"]):
            rows.append(_row("usage_terms.spoken", s["f0"], s["f1"], fps, s["text"], None, "rules.asr", "fact",
                             {"sentence": s["id"]}))
        if SPOKEN_CLAIM.search(s["text"]):
            rows.append(_row("claim.spoken", s["f0"], s["f1"], fps, s["text"], None, "rules.asr", "fact",
                             {"sentence": s["id"]}))
    return rows


def from_reading(reading: dict[str, Any], producer: str, fps: float, duration: float) -> list[dict[str, Any]]:
    from vwb.index.readings import to_seconds
    rows = []
    for h in reading.get("hazards", []) or []:
        t0 = to_seconds(h.get("start"), 0.0) or 0.0
        t1 = to_seconds(h.get("end"), None) or min(duration, t0 + 3.0)
        rows.append(_row(f"{h.get('kind', 'other')}", int(t0 * fps), int(max(t1, t0 + 0.04) * fps), fps,
                         h.get("why", ""), None, producer, "claim", {"why": h.get("why", "")}, quote=h.get("quote", "")))
    return rows


def _row(kind, f0, f1, fps, text, box, producer, status, evidence, quote: str | None = None) -> dict[str, Any]:
    x, y, w, h = box if box else (None, None, None, None)
    return {"kind": kind, "f0": int(f0), "f1": int(f1), "t0": f0 / fps, "t1": f1 / fps, "text": text,
            "quote": quote if quote is not None else text, "x": x, "y": y, "w": w, "h": h,
            "producer": producer, "status": status, "evidence": evidence}
