"""Narration-guided focus: what the viewer should see during each spoken beat.

For slides and graphics, the narrator usually reads or paraphrases what is on screen. We
match each beat's words to the on-screen text tracks (IDF-weighted), lift the matches to
their layout blocks (so a chart comes with its caption), and frame that. With no text match
we fall back to the presenter's face, then the main content block, then saliency.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from vwb.index.asr import normalize_tokens
from vwb.index.store import IndexStore

STOP = set("""a an the of and or to in on for with by at as is are was were be been being it its this that these those
from than then so such can may also into over under per about we we'll will you your our their they them he she his her
which who whom what when where why how not no yes do does did have has had more most less very just only some any all each
""".split())
NON_FOCUS_ROLES = {"logo_text", "job_code", "legal", "footnote", "reference", "usage_terms"}


def content_tokens(text: str) -> list[str]:
    return [t for t in normalize_tokens(text) if t not in STOP and (len(t) > 2 or any(ch.isdigit() for ch in t))]


@dataclass
class Focus:
    box: tuple[float, float, float, float]      # source pixels
    kind: str                                    # text | presenter | content | saliency | frame
    reason: str
    tracks: list[int] = field(default_factory=list)
    min_cap_h: float | None = None               # smallest cap height of focus text, source px
    score: float = 0.0


def union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


class FocusFinder:
    def __init__(self, store: IndexStore, exclude_title: bool = True):
        self.store = store
        m = store.media()
        self.W, self.H = float(m["width"]), float(m["height"])
        self.tracks = store.all("SELECT * FROM text_tracks")
        self.blocks = {b["id"]: b for b in store.all("SELECT * FROM blocks")}
        self.blocks_by_shot: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for b in self.blocks.values():
            self.blocks_by_shot[b["shot"]].append(b)
        self.shots = {s["id"]: s for s in store.all("SELECT * FROM shots")}
        self.faces = store.all("SELECT * FROM faces ORDER BY f")
        self.saliency = store.all("SELECT * FROM saliency ORDER BY f")
        self.exclude_title = exclude_title
        df: Counter[str] = Counter()
        self.tokens: dict[int, list[str]] = {}
        for t in self.tracks:
            toks = content_tokens(t["text"])
            self.tokens[t["id"]] = toks
            df.update(set(toks))
        n = max(1, len(self.tracks))
        self.idf = {tok: math.log(1 + n / c) for tok, c in df.items()}
        self.mean_idf = sum(self.idf.values()) / max(1, len(self.idf))

    # ------------------------------------------------------------------ helpers
    def _active(self, f0: int, f1: int, shot: int) -> list[dict[str, Any]]:
        return [t for t in self.tracks if t["f0"] < f1 and t["f1"] > f0 and t["shot"] == shot]

    def _faces(self, f0: int, f1: int) -> list[dict[str, Any]]:
        return [f for f in self.faces if f0 <= f["f"] < f1]

    def presenter_box(self, f0: int, f1: int) -> tuple[float, float, float, float] | None:
        fs = self._faces(f0, f1)
        if not fs:
            return None
        # median face over the beat, expanded to head and shoulders
        xs = sorted(f["x"] + f["w"] / 2 for f in fs)
        ys = sorted(f["y"] for f in fs)
        ws = sorted(f["w"] for f in fs)
        hs = sorted(f["h"] for f in fs)
        cx, y, w, h = xs[len(xs) // 2], ys[len(ys) // 2], ws[len(ws) // 2], hs[len(hs) // 2]
        bw, bh = 3.4 * w, 3.6 * h
        return (cx - bw / 2, y - 0.75 * h, bw, bh)

    def main_content(self, shot: int, include_title: bool = False) -> tuple[float, float, float, float] | None:
        blocks = [b for b in self.blocks_by_shot.get(shot, []) if b["kind"] != "person"]
        title_ids = {t["block"] for t in self.tracks if t["shot"] == shot and t["role"] == "title"}
        logo_ids = {t["block"] for t in self.tracks if t["shot"] == shot and t["role"] in NON_FOCUS_ROLES}
        footer = [b for b in blocks if b["y"] > 0.86 * self.H or (b["y"] + b["h"] > 0.97 * self.H and b["h"] < 0.08 * self.H)
                  or b["id"] in logo_ids]
        body = [b for b in blocks if b not in footer and (include_title or not self.exclude_title or b["id"] not in title_ids)]
        if not body:
            return None
        return union([(b["x"], b["y"], b["w"], b["h"]) for b in body])

    # ------------------------------------------------------------------ main
    def obstacles(self, f0: int, f1: int, shot: int, exclude: set[int]) -> list[tuple[float, float, float, float]]:
        """Things a window edge must not cut in half: text lines and faces in the beat."""
        out = [(t["x"], t["y"], t["w"], t["h"]) for t in self._active(f0, f1, shot)
               if t["id"] not in exclude and t["role"] != "logo_text"]
        out += [(f["x"], f["y"], f["w"], f["h"]) for f in self._faces(f0, f1)[:4]]
        return out

    def focus(self, f0: int, f1: int, shot: int, text: str) -> Focus:
        words = content_tokens(text)
        wordset = set(words)
        shot_kind = (self.shots.get(shot, {}) or {}).get("kind") or ""
        card = shot_kind in ("section_card", "title_card")
        active = [t for t in self._active(f0, f1, shot) if t["role"] not in NON_FOCUS_ROLES]
        if self.exclude_title and not card:
            active = [t for t in active if t["role"] != "title"]
        scored: list[tuple[float, dict[str, Any]]] = []
        for t in active:
            toks = self.tokens.get(t["id"], [])
            if not toks:
                continue
            total = sum(self.idf.get(k, 1.0) for k in toks)
            hit = sum(self.idf.get(k, 1.0) * (1.6 if any(c.isdigit() for c in k) else 1.0) for k in toks if k in wordset)
            if hit:
                # a one-word label that matches is weak evidence on its own; lines carry more
                scored.append((hit / max(total, 2.5 * self.mean_idf), t))
        if scored:
            by_block: dict[Any, float] = defaultdict(float)
            for sc, t in scored:
                by_block[t["block"] if t["block"] is not None else -t["id"] - 1] += sc
            best_block = max(by_block, key=by_block.get)
            if by_block[best_block] >= 0.45:
                chosen = [t for sc, t in scored if (t["block"] if t["block"] is not None else -t["id"] - 1) == best_block
                          and sc >= 0.2]
                tight = union([(t["x"], t["y"], t["w"], t["h"]) for t in chosen])
                blk = self.blocks.get(best_block) if isinstance(best_block, int) and best_block >= 0 else None
                box = tight
                if blk is not None:
                    barea = blk["w"] * blk["h"]
                    if barea <= 2.5 * tight[2] * tight[3] or (blk["kind"] in ("graphic", "mixed") and barea <= 0.3 * self.W * self.H):
                        box = union([tight, (blk["x"], blk["y"], blk["w"], blk["h"])])
                caps = [t["cap_h"] for t in chosen if t["cap_h"]]
                return Focus(box=box, kind="text", reason=f"narration matches on-screen text: \"{chosen[0]['text'][:60]}\"",
                             tracks=[t["id"] for t in chosen], min_cap_h=min(caps) if caps else None,
                             score=by_block[best_block])
        pres = self.presenter_box(f0, f1)
        if pres and shot_kind in ("presenter", "slide_presenter", "footage"):
            return Focus(box=pres, kind="presenter", reason="no text match; presenter on screen")
        main = self.main_content(shot, include_title=card)
        if main:
            caps = [t["cap_h"] for t in active if t["cap_h"]]
            return Focus(box=main, kind="content", reason="no text match; main content of the shot",
                         min_cap_h=(sorted(caps)[len(caps) // 2] if caps else None))
        if pres:
            return Focus(box=pres, kind="presenter", reason="no text and no content block; presenter")
        sal = [s for s in self.saliency if f0 <= s["f"] < f1]
        if sal:
            return Focus(box=union([(s["x"], s["y"], s["w"], s["h"]) for s in sal]), kind="saliency",
                         reason="saliency (no text, no faces, no blocks)")
        return Focus(box=(0.0, 0.0, self.W, self.H), kind="frame", reason="nothing to anchor on; whole frame")


def split_beats(words: list[dict[str, Any]], soft_s: float = 3.5, hard_s: float = 7.5) -> list[list[dict[str, Any]]]:
    """Beats: sentences, split at a comma once a beat passes soft_s, and anywhere past hard_s."""
    beats: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for w in words:
        cur.append(w)
        text = w["text"]
        dur = cur[-1]["t1"] - cur[0]["t0"]
        if re.search(r"[.!?]$", text) or (dur > soft_s and re.search(r"[,;:]$", text)) or dur > hard_s:
            beats.append(cur)
            cur = []
    if cur:
        if beats and cur[-1]["t1"] - cur[0]["t0"] < 1.0:
            beats[-1].extend(cur)   # a tiny tail joins the previous beat
        else:
            beats.append(cur)
    return beats
