"""Clip validators: they prune plans before any render (spec: "validators prune them").

Each issue names a flag kind from the catalog, so the same words reach the reviewer later.
"""

from __future__ import annotations

from typing import Any

from vwb.clip.candidates import DANGLING_START
from vwb.index.store import IndexStore


def validate_plan(store: IndexStore, outputs: list[dict[str, Any]], dmin: float, dmax: float,
                  max_duration: float) -> dict[str, Any]:
    """outputs: [{id, spans: [[f0, f1], ...]}] in source frames. Returns issues per output."""
    m = store.media()
    fps = float(m["fps"])
    words = store.all("SELECT * FROM words ORDER BY t0")
    sents = store.all("SELECT * FROM sentences ORDER BY id")
    hazards = store.all("SELECT * FROM hazards")
    shots = store.all("SELECT * FROM shots ORDER BY id")
    report: dict[str, Any] = {"outputs": [], "pairs": []}
    for out in outputs:
        issues: list[dict[str, Any]] = []
        spans = [(int(a), int(b)) for a, b in out["spans"]]
        dur = sum(b - a for a, b in spans) / fps
        if dur > max_duration:
            issues.append({"kind": "DURATION", "severity": "BLOCK", "why": f"{dur:.1f}s is over the platform limit {max_duration}s"})
        elif not (dmin - 0.5 <= dur <= dmax + 0.5):
            issues.append({"kind": "DURATION", "severity": "WARN", "why": f"{dur:.1f}s is outside the requested {dmin}-{dmax}s"})
        for a, b in spans:
            ta, tb = a / fps, b / fps
            for w in words:
                if w["t0"] + 0.02 < ta < w["t1"] - 0.02 or w["t0"] + 0.02 < tb < w["t1"] - 0.02:
                    issues.append({"kind": "BOUNDARY-WORD", "severity": "BLOCK",
                                   "why": f"cut at {ta if w['t0'] < ta < w['t1'] else tb:.2f}s is inside \"{w['text']}\""})
            for s in sents:
                inside = max(0.0, min(tb, s["t1"]) - max(ta, s["t0"]))
                if 0.05 < inside < (s["t1"] - s["t0"]) - 0.05:
                    issues.append({"kind": "ATOM-BROKEN", "severity": "HOLD",
                                   "why": f"sentence s{s['id']:03d} is only partly kept: \"{s['text'][:80]}\""})
            first = next((s for s in sents if s["t0"] >= ta - 0.05), None)
            if first and DANGLING_START.match(first["text"].strip()):
                issues.append({"kind": "SELF-CONTAINED", "severity": "WARN",
                               "why": f"opens with \"{first['text'].split()[0]}\", which leans on earlier context"})
            kept_shots = [s for s in shots if s["f0"] < b and s["f1"] > a]
            for s in kept_shots:
                if (s["kind"] or "") in ("disclaimer", "logo_card"):
                    issues.append({"kind": "FURNITURE", "severity": "INFO",
                                   "why": f"includes the {s['kind']} shot {s['id']}"})
        claims = [h for h in hazards if h["kind"].startswith("claim") and any(h["f0"] < b and h["f1"] > a for a, b in spans)]
        report["outputs"].append({"id": out["id"], "duration": round(dur, 2), "claims_inside": len(claims),
                                  "issues": issues})
    for i, a in enumerate(outputs):
        for b in outputs[i + 1:]:
            ov = sum(max(0, min(y1, z1) - max(y0, z0)) for y0, y1 in a["spans"] for z0, z1 in b["spans"]) / fps
            if ov > 0.5:
                report["pairs"].append({"a": a["id"], "b": b["id"], "overlap_s": round(ov, 2)})
    report["ok"] = not any(i["severity"] == "BLOCK" for o in report["outputs"] for i in o["issues"])
    return report
