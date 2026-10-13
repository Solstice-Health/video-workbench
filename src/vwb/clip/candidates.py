"""Clip candidates: every sentence-aligned span in the target length, scored with reasons.

The scorer is deterministic and explains itself. It nominates; planners (the job lead and
the workers) choose. Features: length fit, a clean start (no dangling "These", "However"),
a clean end, alignment with shot cuts, coherence with the readings' sections, support from
the readings' proposed moments, and a hook in the first sentence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from vwb.index.store import IndexStore

DANGLING_START = re.compile(r"^(and|but|so|or|however|also|these|this|that|those|it|they|such|then|"
                            r"additionally|furthermore|moreover|as a result|therefore|thus|second|third|finally|"
                            r"in summary|in addition|as such|meanwhile|on behalf)\b", re.I)
WEAK_START = re.compile(r"^(first|welcome|today|we'll|we will|let's)\b", re.I)
SETUP_END = re.compile(r"(:|\bthree points\b|\bthe following\b|\bas follows\b)\W*$", re.I)
LEADIN = re.compile(r"^(first|second|third|finally|next|now)\b|\bcan also\b|\blet's\b", re.I)
HOOK = re.compile(r"(\d+(\.\d+)?\s?%|\b\d+(\.\d+)?-fold\b|\bmost common\b|\bmillion\b|\bincreased? risk\b|"
                  r"\bhigher\b|\?$|\bonly about\b|\bremain low\b|\bexpanded\b|\bbeyond\b|\blasting\b)", re.I)
SIGNOFF = re.compile(r"(thank you for watching|on behalf of)", re.I)


@dataclass
class Candidate:
    s0: int                       # first sentence id
    s1: int                       # last sentence id (inclusive)
    t0: float
    t1: float
    text: str
    score: float = 0.0
    features: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    shots: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.t1 - self.t0

    def to_dict(self) -> dict[str, Any]:
        return {"sentences": [self.s0, self.s1], "t0": round(self.t0, 3), "t1": round(self.t1, 3),
                "duration": round(self.duration, 2), "score": round(self.score, 3), "text": self.text,
                "features": {k: round(v, 3) for k, v in self.features.items()}, "reasons": self.reasons,
                "sections": self.sections, "shots": self.shots}


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def generate(store: IndexStore, dmin: float = 15.0, dmax: float = 60.0, target: float = 30.0,
             exclude_kinds: tuple[str, ...] = ("disclaimer", "logo_card", "title_card")) -> list[Candidate]:
    sents = store.all("SELECT * FROM sentences ORDER BY id")
    shots = store.all("SELECT * FROM shots ORDER BY id")
    sections = store.all("SELECT * FROM sections")
    moments = store.all("SELECT * FROM moments")
    cuts = [s["t0"] for s in shots] + [shots[-1]["t1"]] if shots else []
    shot_kind = {s["id"]: (s["kind"] or "") for s in shots}

    def nearest_cut(t: float) -> float:
        return min((abs(t - c) for c in cuts), default=9.9)

    out: list[Candidate] = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            t0, t1 = sents[i]["t0"], sents[j]["t1"]
            dur = t1 - t0
            if dur > dmax:
                break
            if dur < dmin:
                continue
            group = sents[i:j + 1]
            text = " ".join(s["text"] for s in group)
            c = Candidate(i, j, t0, t1, text)
            f, r = c.features, c.reasons
            f["length"] = math.exp(-((dur - target) / 14.0) ** 2)
            first, last = group[0]["text"].strip(), group[-1]["text"].strip()
            if DANGLING_START.match(first):
                f["start"] = -1.0
                r.append(f"starts with a dangling word: \"{first.split()[0]}\"")
            elif WEAK_START.match(first):
                f["start"] = -0.3
                r.append("weak opening (greeting or agenda)")
            else:
                f["start"] = 0.5
            last_dur = group[-1]["t1"] - group[-1]["t0"]
            if SETUP_END.search(last):
                f["end"] = -1.0
                r.append("ends on a set-up that needs what follows")
            elif j > i and (LEADIN.search(last) or last_dur < 3.0):
                f["end"] = -0.8
                r.append("ends on a lead-in to the next idea")
            elif not re.search(r"[.!?][\"')]*$", last):
                f["end"] = -0.5
                r.append("ends without terminal punctuation")
            else:
                f["end"] = 0.4
            f["cut_align"] = 0.5 * math.exp(-nearest_cut(t0) / 0.8) + 0.5 * math.exp(-nearest_cut(t1) / 1.2)
            c.shots = sorted({s["shot"] for s in group})
            kinds = [shot_kind.get(s, "") for s in c.shots]
            if any(k in exclude_kinds for k in kinds):
                f["furniture"] = -0.6
                r.append("spans title, disclaimer or logo cards")
            secs = {}
            for sec in sections:
                ov = _overlap(t0, t1, sec["t0"], sec["t1"])
                if ov > 1.0:
                    secs.setdefault(sec["producer"].split(":")[0], []).append(sec["title"])
            n_secs = max((len(v) for v in secs.values()), default=1)
            c.sections = sorted({t for v in secs.values() for t in v})
            f["coherence"] = 0.4 if n_secs <= 1 else (0.0 if n_secs == 2 else -0.6)
            best_m = 0.0
            for m in moments:
                inter = _overlap(t0, t1, m["t0"], m["t1"])
                union = max(t1, m["t1"]) - min(t0, m["t0"])
                if union > 0:
                    best_m = max(best_m, (inter / union) * ((m["score"] or 5) / 10.0))
            f["moment_support"] = best_m
            f["hook"] = 0.5 if HOOK.search(first) else 0.0
            if f["hook"]:
                r.append("opens on a hook (statistic, risk or change)")
            if SIGNOFF.search(text) and j == len(sents) - 1:
                f["signoff"] = -0.2
            c.score = (1.2 * f["length"] + f["start"] + f["end"] + 0.8 * f["cut_align"] + f.get("furniture", 0.0)
                       + f["coherence"] + 1.5 * f["moment_support"] + f["hook"] + f.get("signoff", 0.0))
            out.append(c)
    out.sort(key=lambda c: -c.score)
    return out


def select_set(cands: list[Candidate], n: int, max_overlap: float = 0.25) -> list[Candidate]:
    """Best total score with exactly n non-overlapping candidates (weighted interval scheduling).
    If the source cannot hold n disjoint spans, fall back to greedy picks with limited overlap."""
    items = sorted(cands, key=lambda c: c.t1)
    m = len(items)
    if m == 0:
        return []
    ends = [c.t1 for c in items]
    import bisect
    prev = [bisect.bisect_right(ends, items[i].t0 + 1e-6) for i in range(m)]  # items[0:prev[i]] end before i starts
    neg = float("-inf")
    dp = [[0.0] * (m + 1)] + [[neg] * (m + 1) for _ in range(n)]
    for k in range(1, n + 1):
        for i in range(1, m + 1):
            take = dp[k - 1][prev[i - 1]] + items[i - 1].score if dp[k - 1][prev[i - 1]] != neg else neg
            dp[k][i] = max(dp[k][i - 1], take)
    if dp[n][m] != neg:
        chosen, k, i = [], n, m
        while k > 0 and i > 0:
            if dp[k][i] == dp[k][i - 1]:
                i -= 1
            else:
                chosen.append(items[i - 1])
                i, k = prev[i - 1], k - 1
        return sorted(chosen, key=lambda c: c.t0)
    for limit in (max_overlap, 0.4, 0.6, 1.0):
        picked: list[Candidate] = []
        for c in cands:
            if all(_overlap(c.t0, c.t1, d.t0, d.t1) <= limit * min(c.duration, d.duration) for d in picked):
                picked.append(c)
            if len(picked) == n:
                return sorted(picked, key=lambda c: c.t0)
    return sorted(picked, key=lambda c: c.t0)
