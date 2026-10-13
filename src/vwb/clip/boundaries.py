"""Cut refinement: put each cut in a speech gap, at an energy minimum, on a shot cut if one is near.

A cut inside a word is BOUNDARY-WORD (BLOCK). A flash frame (one to three frames of the next
shot before an end cut) is SEAM-ROUGH. Times in, half-open frame ranges out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from vwb.index.store import IndexStore


@dataclass
class Cut:
    f_in: int
    f_out: int
    notes: list[str] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"f_in": self.f_in, "f_out": self.f_out, "notes": self.notes, "risks": self.risks}


class Refiner:
    def __init__(self, store: IndexStore, index_dir: Path):
        self.store = store
        m = store.media()
        self.fps = float(m["fps"])
        self.n_frames = int(m["n_frames"])
        self.words = store.all("SELECT * FROM words ORDER BY t0")
        self.cuts = [s["t0"] for s in store.all("SELECT t0 FROM shots ORDER BY id")][1:]
        rms_path = Path(index_dir) / "rms_db.npy"
        self.rms_t, self.rms_db = (np.load(rms_path) if rms_path.exists() else (np.zeros(0), np.zeros(0)))

    def _energy_min(self, a: float, b: float, prefer: float) -> tuple[float, float]:
        """Lowest-energy time in [a, b]; ties go to the time closest to `prefer`."""
        if b <= a or len(self.rms_t) == 0:
            return prefer, -99.0
        sel = (self.rms_t >= a) & (self.rms_t <= b)
        if not np.any(sel):
            return prefer, -99.0
        ts, db = self.rms_t[sel], self.rms_db[sel]
        floor = float(db.min())
        quiet = ts[db <= floor + 3.0]
        t = float(quiet[np.argmin(np.abs(quiet - prefer))])
        return t, floor

    def _find_pause(self, onset: float, gap_lo: float) -> tuple[float, float]:
        """Where ASR word times touch, the audio usually still has a pause. Return (onset, gap_lo)
        around the quietest 10 ms run within 350 ms of the boundary, if it is clearly quiet."""
        if len(self.rms_t) == 0:
            return onset, gap_lo
        a, b = gap_lo - 0.35, onset + 0.2
        sel = (self.rms_t >= a) & (self.rms_t <= b)
        if not np.any(sel):
            return onset, gap_lo
        ts, db = self.rms_t[sel], self.rms_db[sel]
        speech = float(np.median(self.rms_db[(self.rms_t > a - 2) & (self.rms_t < b + 2)]))
        quiet = db <= min(-42.0, speech - 18.0)
        if not np.any(quiet):
            return onset, gap_lo
        # the longest quiet run inside the window
        best, run, best_run = None, [], []
        for t, q in zip(ts, quiet):
            if q:
                run.append(t)
                if len(run) > len(best_run):
                    best_run = list(run)
            else:
                run = []
        if len(best_run) < 2:
            return onset, gap_lo
        return best_run[-1] + 0.005, best_run[0] - 0.005

    def _word_at(self, t: float) -> dict[str, Any] | None:
        for w in self.words:
            if w["t0"] + 0.02 < t < w["t1"] - 0.02:
                return w
        return None

    def refine(self, t0: float, t1: float) -> Cut:
        notes: list[str] = []
        risks: list[dict[str, Any]] = []
        first = next((w for w in self.words if w["t0"] >= t0 - 0.05), None)
        prev = max((w for w in self.words if w["t1"] <= (first["t0"] if first else t0) + 0.01), key=lambda w: w["t1"], default=None)
        last = max((w for w in self.words if w["t1"] <= t1 + 0.05), key=lambda w: w["t1"], default=None)
        nxt = next((w for w in self.words if last and w["t0"] >= last["t1"] - 0.01 and w is not last), None)

        # ---- in point: inside the gap before the first word, just ahead of its onset
        onset = first["t0"] if first else t0
        gap_lo = prev["t1"] if prev else max(0.0, onset - 1.0)
        if onset - gap_lo < 0.12:
            # word timings touch; find the real pause in the audio near the boundary
            onset, gap_lo = self._find_pause(onset, gap_lo)
        lo, hi = max(gap_lo + 0.02, onset - 0.6), onset - 0.06
        t_in, floor_in = self._energy_min(lo, hi, prefer=onset - 0.18) if hi > lo else (max(gap_lo, onset - 0.04), -99.0)
        snap = [c for c in self.cuts if gap_lo + 0.02 <= c <= onset - 0.03 and abs(c - onset) <= 0.8]
        if snap:
            t_in = max(snap)
            notes.append(f"in point on the shot cut at {t_in:.3f}s")
        if onset - gap_lo < 0.1:
            risks.append({"kind": "BOUNDARY-WORD", "where": "in", "t": round(t_in, 3),
                          "why": f"only {1000 * (onset - gap_lo):.0f} ms between words"})
        f_in = int(math.floor(t_in * self.fps + 1e-6))
        # flash-frame guard: a shot cut just after the in point drags in a few frames of the old shot
        for c in self.cuts:
            fc = int(round(c * self.fps))
            if f_in < fc <= f_in + 3 and c <= onset - 0.03:
                f_in = fc
                notes.append("moved in point onto a shot cut to avoid a flash frame")

        # ---- out point: after the last word's tail, before the next word
        end = last["t1"] if last else t1
        gap_hi = nxt["t0"] if nxt else min(self.n_frames / self.fps, end + 1.5)
        if gap_hi - end < 0.12:
            gap_hi, end = self._find_pause(gap_hi, end)
        lo, hi = end + 0.06, min(gap_hi - 0.05, end + 0.7)
        t_out, floor_out = self._energy_min(lo, hi, prefer=min(end + 0.35, hi)) if hi > lo else (min(gap_hi, end + 0.04), -99.0)
        snap = [c for c in self.cuts if end + 0.05 <= c <= gap_hi - 0.02 and c - end <= 1.0]
        if snap:
            t_out = min(snap)
            notes.append(f"out point on the shot cut at {t_out:.3f}s")
        if gap_hi - end < 0.1:
            risks.append({"kind": "BOUNDARY-WORD", "where": "out", "t": round(t_out, 3),
                          "why": f"only {1000 * (gap_hi - end):.0f} ms between words"})
        f_out = int(math.ceil(t_out * self.fps - 1e-6))
        for c in self.cuts:
            fc = int(round(c * self.fps))
            if fc < f_out <= fc + 3 and c >= end + 0.03:
                f_out = fc
                notes.append("moved out point onto a shot cut to avoid a flash frame")
        f_out = min(f_out, self.n_frames)
        if self._word_at(f_in / self.fps) or self._word_at(f_out / self.fps):
            risks.append({"kind": "BOUNDARY-WORD", "where": "in" if self._word_at(f_in / self.fps) else "out",
                          "t": round((f_in if self._word_at(f_in / self.fps) else f_out) / self.fps, 3),
                          "why": "cut lands inside a word"})
        notes.append(f"gaps: before {1000 * (onset - gap_lo):.0f} ms, after {1000 * (gap_hi - end):.0f} ms; "
                     f"energy at cuts {floor_in:.0f}/{floor_out:.0f} dBFS")
        return Cut(f_in, max(f_out, f_in + 1), notes, risks)
