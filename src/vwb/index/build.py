"""Build the edit index for one source: eager facts first, readings beside them.

Stages run in dependency order and each is cached by (version, settings). Model calls run
in a thread pool while local compute (decode, Vision, Whisper) runs in the main thread.

    media -> audio -> shots -> vision (OCR, faces, saliency) -> text tracks -> layout
          -> asr (reader 1 + reader 2 -> consensus) -> hazards -> marks
    reading.video (Gemini) runs from the start; reading.text (GPT) after asr and text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from vwb.config import lever_serves, slot
from vwb.gateway import Gateway
from vwb.index import asr, dossier, hazards, layout, marks, ocr, readings, vision
from vwb.index.shots import detect_shots
from vwb.index.store import IndexStore
from vwb.media import audio as audio_mod
from vwb.media.decode import read_frames, sample_frames
from vwb.media.frames import contact_sheet, jpeg_bytes, resize_to_width
from vwb.media.probe import probe
from vwb.util.hashing import sha256_file, sha256_json
from vwb.util.jsonio import dumps, read_json, write_json
from vwb.util.log import log
from vwb.util.procs import ffmpeg
from vwb.workspace import SourceRef, Workspace

INDEX_VERSION = "1.0"
SAMPLE_FPS = 5.0          # faces and saliency
OCR_MAX_GAP_S = 1.0       # OCR at least once a second, and on every visible change


class IndexBuilder:
    def __init__(self, ws: Workspace, source: SourceRef, gw: Gateway | None = None, force: bool = False,
                 progress: Callable[[str], None] | None = None):
        self.ws, self.src = ws, source
        self.gw = gw or Gateway(ws, actor="index")
        self.force = force
        self.store = IndexStore(ws.index / "index.sqlite")
        self.progress = progress or (lambda msg: log.info("index: %s", msg))
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="index-api")
        self.info: dict[str, Any] = {}
        self.fps = 30.0
        self._cascade = False

    # ---------------------------------------------------------------- helpers
    def _stage(self, name: str, settings: Any, fn: Callable[[], str | None], optional: bool = False) -> None:
        # stages run in dependency order: once one re-runs, every later stage re-runs too
        if not self.force and not self._cascade and self.store.stage_done(name, INDEX_VERSION, settings):
            self.progress(f"{name}: cached")
            return
        self._cascade = self._cascade or name not in ("media", "reading.video", "reading.text")
        t0 = time.time()
        try:
            note = fn() or ""
        except Exception as exc:
            if not optional:
                raise
            # a missing reading degrades the index; it never stops it (recorded as RUN-DEGRADED)
            log.warning("index stage %s failed: %s", name, exc)
            self._degraded(name, str(exc))
            return
        self.store.mark_stage(name, INDEX_VERSION, settings, time.time() - t0, note)
        self.progress(f"{name}: done in {time.time() - t0:.1f}s {note}")

    def _degraded(self, stage: str, error: str) -> None:
        from vwb.workspace import flags
        from vwb.workspace.ledger import Ledger
        flag = flags.make("RUN-DEGRADED", f"Index stage {stage} failed; the index runs without it",
                          subject=f"index:{stage}", evidence={"error": error[:500]}, raised_by="index")
        Ledger(self.ws.ledger / "ledger.jsonl").append("flag.raised", "index", flag.to_dict())

    def _shot_of(self, frame: int) -> int:
        for s in self._shots:
            if s["f0"] <= frame < s["f1"]:
                return s["id"]
        return self._shots[-1]["id"] if self._shots else -1

    # ---------------------------------------------------------------- stages
    def run(self) -> dict[str, Any]:
        t_start = time.time()
        self._stage("media", {"sha": self.src.sha256}, self.stage_media)
        self.info = json.loads(self.store.media()["info"])
        self.fps = float(self.info["fps"])

        reading_future: Future | None = None
        if not self.force and self.store.stage_done("reading.video", INDEX_VERSION, {"slot": slot("reading.video")}):
            self.progress("reading.video: cached")
        else:
            reading_future = self.pool.submit(readings.video_reading, self.gw, self.src.path, self.info["duration"])

        self._stage("audio", {"v": 1}, self.stage_audio)
        reader2_future: Future | None = None
        asr_settings = {"r1": slot("asr.reader1"), "r2": slot("asr.reader2") if lever_serves("index.asr_reader2") else None, "v": 4}
        if lever_serves("index.asr_reader2") and (self.force or not self.store.stage_done("asr", INDEX_VERSION, asr_settings)):
            reader2_future = self.pool.submit(asr.transcribe_reader2, self.gw, self.ws.index / "audio16k.wav")

        self._stage("shots", {"engine": "adaptive", "v": 1}, self.stage_shots)
        self._shots = self.store.all("SELECT * FROM shots ORDER BY id")
        self._stage("vision", {"fps": SAMPLE_FPS, "ocr_gap": OCR_MAX_GAP_S, "v": 2}, self.stage_vision)
        self._stage("text", {"reader2": lever_serves("index.ocr_reader2"), "v": 5}, self.stage_text)
        self._stage("layout", {"v": 3}, self.stage_layout)
        self._stage("asr", asr_settings, lambda: self.stage_asr(reader2_future))

        self._stage("text.reader3", {"slot": slot("ocr.reader3"), "v": 1}, self.stage_reader3, optional=True)
        if reading_future is not None:
            self._stage("reading.video", {"slot": slot("reading.video")},
                        lambda: self.stage_reading_video(reading_future.result()), optional=True)
        self._stage("hazards", {"v": 3}, self.stage_hazards)
        self._stage("marks", {"v": 5}, self.stage_marks)
        if lever_serves("index.second_reading"):
            # keyed by the transcript text, so it re-reads only when what was said changes
            said = sha256_json([r["text"] for r in self.store.all("SELECT text FROM sentences ORDER BY id")])
            cascade, self._cascade = self._cascade, False
            self._stage("reading.text", {"slot": slot("reading.text"), "transcript": said}, self.stage_reading_text,
                        optional=True)
            self._cascade = cascade
        self._stage("disagreements", {"v": 1}, self.stage_disagreements)

        text = dossier.build(self.store, f"{self.src.media_id}@{self.src.rev}")
        (self.ws.index / "dossier.md").write_text(text)
        self.pool.shutdown(wait=False)
        summary = {"media": self.src.ref, "seconds": round(time.time() - t_start, 1),
                   "counts": {t: self.store.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
                              for t in ("shots", "sentences", "words", "text_tracks", "faces", "blocks",
                                        "hazards", "marks", "moments", "sections")}}
        self.progress(f"index built in {summary['seconds']}s: {summary['counts']}")
        return summary

    def stage_media(self) -> str:
        info = probe(self.src.path).to_dict()
        self.store.set_media(self.src.media_id, self.src.rev, self.src.path, info)
        return f"{info['width']}x{info['height']} {info['duration']:.1f}s"

    def stage_audio(self) -> str:
        wav = self.ws.index / "audio16k.wav"
        ffmpeg("-i", self.src.path, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav), expect=[wav])
        pcm = audio_mod.load_pcm(wav, sr=16000)
        times, db = audio_mod.rms_envelope(pcm, 16000, hop_ms=10)
        np.save(self.ws.index / "rms_db.npy", np.stack([times, db]).astype(np.float32))
        sil = audio_mod.silences(wav, noise_db=-42, min_dur=0.18)
        self.store.replace_rows("silences", [{"t0": a, "t1": b} for a, b in sil])
        loud = audio_mod.loudness(self.src.path)
        self.store.add_observation(media_id=self.src.media_id, media_rev=self.src.rev, kind="audio.loudness",
                                   f0=0, f1=self.info["n_frames"], value=loud, producer="ffmpeg.ebur128",
                                   status="fact", text=f"I={loud['integrated_lufs']} LUFS TP={loud['true_peak_dbtp']}")
        return f"{len(sil)} silences, I={loud['integrated_lufs']} LUFS"

    def stage_shots(self) -> str:
        shots = detect_shots(self.src.path, self.info["n_frames"], self.fps)
        self.store.replace_rows("shots", shots)
        return f"{len(shots)} shots"

    def stage_vision(self) -> str:
        """One decode pass at SAMPLE_FPS: faces, people and saliency on every sample, OCR on changes."""
        step = max(1, int(round(self.fps / SAMPLE_FPS)))
        small = (960, int(round(960 * self.info["height"] / self.info["width"])))
        sx = self.info["width"] / small[0]
        pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="vision")
        pending: dict[int, list[Future]] = {}
        results_small: dict[int, vision.VisionResult] = {}
        results_ocr: dict[int, list[dict[str, Any]]] = {}
        last_thumb, last_ocr = None, -10**9
        shot_starts = {s["f0"] for s in self._shots}

        def drain(limit: int) -> None:
            while len(pending) > limit:
                idx = min(pending)
                futs = pending.pop(idx)
                results_small[idx] = futs[0].result()
                if len(futs) > 1:
                    res = futs[1].result()
                    results_ocr[idx] = [{"text": ln.text, "conf": ln.conf, "box": ln.box} for ln in res.lines
                                        if ln.text.strip()]

        for idx, rgb in sample_frames(self.src.path, step):
            thumb = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), (96, 54), interpolation=cv2.INTER_AREA)
            diff = 999.0 if last_thumb is None else float(np.mean(np.abs(thumb.astype(np.int16) - last_thumb.astype(np.int16))))
            near_cut = any(0 <= idx - s < step for s in shot_starts)
            do_ocr = diff > 1.6 or (idx - last_ocr) >= OCR_MAX_GAP_S * self.fps or near_cut
            small_rgb = cv2.resize(rgb, small, interpolation=cv2.INTER_AREA)
            futs = [pool.submit(vision.analyze, small_rgb, ocr=False)]
            if do_ocr:
                futs.append(pool.submit(vision.analyze, rgb.copy(), faces=False, humans=False, saliency=False))
                last_thumb, last_ocr = thumb, idx
            pending[idx] = futs
            drain(24)
        drain(0)
        pool.shutdown(wait=True)

        faces, persons, sal = [], [], []
        prev: list[dict[str, Any]] = []
        next_track = 0
        for idx in sorted(results_small):
            res = results_small[idx]
            cur = []
            for box, conf in res.faces:
                b = [v * sx for v in box]
                match = max(prev, key=lambda p: ocr.iou(p["box"], b), default=None)
                if match is not None and ocr.iou(match["box"], b) > 0.25:
                    track = match["track"]
                else:
                    track, next_track = next_track, next_track + 1
                cur.append({"box": b, "track": track})
                faces.append({"f": idx, "x": b[0], "y": b[1], "w": b[2], "h": b[3], "conf": conf, "track": track})
            prev = cur
            for box, conf in res.humans:
                b = [v * sx for v in box]
                persons.append({"f": idx, "x": b[0], "y": b[1], "w": b[2], "h": b[3], "conf": conf})
            for box, conf in res.salient:
                b = [v * sx for v in box]
                sal.append({"f": idx, "x": b[0], "y": b[1], "w": b[2], "h": b[3], "conf": conf})
        self.store.replace_rows("faces", faces)
        self.store.replace_rows("persons", persons)
        self.store.replace_rows("saliency", sal)
        samples = [(idx, results_ocr[idx]) for idx in sorted(results_ocr)]
        (self.ws.index / "ocr_samples.json").write_text(dumps({"step": step, "samples": samples}, None))
        return f"{len(results_small)} samples, {len(samples)} OCR frames, {len(faces)} face boxes"

    def stage_text(self) -> str:
        data = json.loads((self.ws.index / "ocr_samples.json").read_text())
        samples = [(int(i), lines) for i, lines in data["samples"]]
        bounds = sorted({s["f0"] for s in self._shots} | {self.info["n_frames"]})
        tracks = ocr.build_tracks(samples, int(OCR_MAX_GAP_S * self.fps), self.info["n_frames"], bounds)
        tracks = [t for t in tracks if len(t["text"]) >= 1]
        median_h = float(np.median([t["h"] for t in tracks if not ocr.is_rotated(t)])) if tracks else 20.0
        reader2 = ocr.Reader2() if lever_serves("index.ocr_reader2") else None
        assign = ocr.cover_frames(tracks) if reader2 else {}
        lines2: dict[int, list[dict[str, Any]]] = {}
        if reader2:
            frames = read_frames(self.src.path, sorted(set(assign.values())))
            lines2 = {f: reader2.read_frame(rgb) for f, rgb in frames.items()}
        rows = []
        for i, t in enumerate(tracks):
            t["id"] = i
            t["shot"] = self._shot_of((t["f0"] + t["f1"]) // 2)
            role, regulated = ocr.classify(t, self.info["width"], self.info["height"], median_h)
            text2 = ocr.match_reader2((t["x"], t["y"], t["w"], t["h"]), lines2.get(assign.get(i), [])) if reader2 else ""
            rows.append({"id": i, "f0": t["f0"], "f1": t["f1"], "t0": t["f0"] / self.fps, "t1": t["f1"] / self.fps,
                         "x": t["x"], "y": t["y"], "w": t["w"], "h": t["h"], "text": t["text"], "text2": text2,
                         "agree": int(ocr.readers_agree(t["text"], text2)), "conf": t["conf"],
                         "cap_h": round((t["w"] if ocr.is_rotated(t) else t["h"]) * 0.68, 1), "role": role,
                         "regulated": int(regulated),
                         "shot": t["shot"], "block": None, "n_seen": t["n_seen"]})
        self.store.replace_rows("text_tracks", rows)
        n_agree = sum(r["agree"] for r in rows)
        return f"{len(rows)} text tracks, readers agree on {n_agree}, reader 2 read {len(lines2)} frames"

    def stage_reader3(self) -> str:
        """A vision model reads the lines the OCR engines disagree on; two of three readings settle a line."""
        from vwb.index import reader3
        rows = self.store.all("SELECT * FROM text_tracks WHERE agree=0 AND length(text) >= 2")
        if not rows:
            return "no disagreements"
        frames = read_frames(self.src.path, sorted({(r["f0"] + r["f1"]) // 2 for r in rows}))
        crops = [reader3.crop_line(frames[(r["f0"] + r["f1"]) // 2], (r["x"], r["y"], r["w"], r["h"])) for r in rows]
        texts = reader3.read(self.gw, crops)
        settled = 0
        con = self.store.connect()
        with con:
            for r, t3 in zip(rows, texts, strict=True):
                text, agreed = reader3.settle(r["text"], r["text2"] or "", t3)
                settled += agreed
                con.execute("UPDATE text_tracks SET text3=?, text=?, agree=? WHERE id=?", (t3, text, int(agreed), r["id"]))
        return f"{len(rows)} disagreeing lines read again, {settled} settled two of three"

    def _rep_frames(self) -> dict[int, int]:
        """Representative frame per shot: the OCR sample with the most text, late in the shot."""
        data = json.loads((self.ws.index / "ocr_samples.json").read_text())
        best: dict[int, tuple[int, int]] = {}
        for idx, lines in data["samples"]:
            sid = self._shot_of(int(idx))
            key = (len(lines), int(idx))
            if sid not in best or key >= best[sid]:
                best[sid] = key
        reps = {}
        for s in self._shots:
            reps[s["id"]] = best[s["id"]][1] if s["id"] in best else (s["f0"] + s["f1"]) // 2
        return reps

    def stage_layout(self) -> str:
        reps = self._rep_frames()
        frames = read_frames(self.src.path, list(reps.values()))
        tracks = self.store.all("SELECT * FROM text_tracks")
        faces = self.store.all("SELECT * FROM faces")
        blocks, n = [], 0
        for s in self._shots:
            idx = reps[s["id"]]
            tboxes = [(t["x"], t["y"], t["w"], t["h"]) for t in tracks if t["f0"] <= idx < t["f1"]]
            fboxes = [(f["x"], f["y"], f["w"], f["h"]) for f in faces if s["f0"] <= f["f"] < s["f1"]]
            seg = layout.segment(frames[idx], tboxes, fboxes[:3])
            for b in seg["blocks"]:
                inside = [t for t in tracks if t["f0"] <= idx < t["f1"] and layout._center_in((t["x"], t["y"], t["w"], t["h"]), (b["x"], b["y"], b["w"], b["h"]))]
                blocks.append({"id": n, "shot": s["id"], "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"],
                               "kind": b["kind"], "area": b["area"],
                               "text": " / ".join(t["text"] for t in sorted(inside, key=lambda t: (t["y"], t["x"])))[:600]})
                for t in inside:
                    self.store.connect().execute("UPDATE text_tracks SET block=? WHERE id=?", (n, t["id"]))
                n += 1
            n_text = sum(1 for t in tracks if t["shot"] == s["id"])
            self.store.connect().execute("UPDATE shots SET bg=?, n_text=?, has_face=? WHERE id=?",
                                         (dumps(seg["bg"], None), n_text, int(bool(fboxes)), s["id"]))
        self.store.connect().commit()
        self.store.replace_rows("blocks", blocks)
        (self.ws.index / "rep_frames.json").write_text(dumps(reps, None))
        return f"{len(blocks)} blocks over {len(self._shots)} shots"

    def stage_asr(self, reader2_future: Future | None) -> str:
        screen_text = " ".join(t["text"] for t in self.store.all("SELECT text FROM text_tracks"))
        glossary = asr.glossary_from_text([screen_text])
        words1 = asr.transcribe_reader1(self.ws.index / "audio16k.wav", glossary, slot("asr.reader1")["model"])
        text2 = ""
        cache = self.ws.index / "asr_reader2.json"
        audio_sha = sha256_file(self.ws.index / "audio16k.wav")
        cached = read_json(cache, {}) or {}
        if reader2_future is not None:
            try:
                text2 = reader2_future.result()
            except Exception as exc:  # noqa: BLE001 - a missing second reader is recorded, not fatal
                log.warning("asr reader 2 failed: %s", exc)
                self._degraded("asr.reader2", str(exc))
        elif cached.get("audio_sha256") == audio_sha and cached.get("text", "").strip():
            text2 = cached["text"]
        elif lever_serves("index.asr_reader2"):
            text2 = asr.transcribe_reader2(self.gw, self.ws.index / "audio16k.wav")
        if text2.strip():
            write_json(cache, {"audio_sha256": audio_sha, "model": slot("asr.reader2")["model"], "text": text2})
        elif lever_serves("index.asr_reader2"):
            self._degraded("asr.reader2", "second transcript reader returned nothing")
        (self.ws.index / "asr_reader1.json").write_text(dumps(words1))
        words = asr.consensus(words1, text2, screen_text) if text2 else [dict(w, agree=0, alt=None, chosen_from="reader1") for w in words1]
        rms = np.load(self.ws.index / "rms_db.npy")
        moved = asr.snap_to_pauses(words, rms[0], rms[1])
        sents = asr.sentences(words)
        word_rows, sent_rows = [], []
        for si, s in enumerate(sents):
            f0, f1 = int(s["t0"] * self.fps), max(int(s["t0"] * self.fps) + 1, int(round(s["t1"] * self.fps)))
            sent_rows.append({"id": si, "f0": f0, "f1": f1, "t0": s["t0"], "t1": s["t1"], "text": s["text"],
                              "w0": s["w0"], "w1": s["w1"], "agree": round(s["agree"], 3),
                              "shot": self._shot_of((f0 + f1) // 2)})
            for wi in range(s["w0"], s["w1"]):
                w = words[wi]
                word_rows.append({"i": wi, "f0": int(w["t0"] * self.fps), "f1": max(int(w["t0"] * self.fps) + 1, int(round(w["t1"] * self.fps))),
                                  "t0": w["t0"], "t1": w["t1"], "text": w["text"], "norm": asr.norm_word(w["text"]),
                                  "conf": w.get("conf", 0.0), "agree": w["agree"], "alt": w.get("alt"),
                                  "chosen_from": w.get("chosen_from"), "sentence": si})
        self.store.replace_rows("words", word_rows)
        self.store.replace_rows("sentences", sent_rows)
        disagree = sum(1 for w in word_rows if not w["agree"])
        return f"{len(word_rows)} words, {len(sent_rows)} sentences, {disagree} words disagree, {moved} boundaries snapped to pauses"

    def stage_reading_video(self, data: dict[str, Any]) -> str:
        self._store_reading(data, producer=f"reading.video:{data.get('_model', '?')}", scope="video")
        # shot kinds and titles from the reading, matched by time overlap (claims)
        for s in self._shots:
            best, best_ov = None, 0.0
            for rs in data.get("shots", []) or []:
                a = readings.to_seconds(rs.get("start"), 0.0) or 0.0
                b = readings.to_seconds(rs.get("end"), a) or a
                ov = max(0.0, min(b, s["t1"]) - max(a, s["t0"]))
                if ov > best_ov:
                    best, best_ov = rs, ov
            if best:
                self.store.connect().execute("UPDATE shots SET kind=?, title=?, description=? WHERE id=?",
                                             (best.get("kind"), best.get("title"), best.get("description"), s["id"]))
        self.store.connect().commit()
        return f"{len(data.get('moments', []))} moments, {len(data.get('hazards', []))} hazards"

    def _store_reading(self, data: dict[str, Any], producer: str, scope: str) -> None:
        dur = self.info["duration"]
        family = producer.split(":")[0]
        con = self.store.connect()
        with con:  # a re-read replaces this reader's rows; it never piles up copies
            con.execute("DELETE FROM readings WHERE producer=? AND scope=?", (family, scope))
            con.execute("DELETE FROM sections WHERE producer LIKE ?", (family + ":%",))
            con.execute("DELETE FROM moments WHERE producer LIKE ?", (family + ":%",))
        self.store.insert_rows("readings", [{"producer": producer.split(":")[0], "model": data.get("_model"), "scope": scope,
                                             "f0": 0, "f1": self.info["n_frames"], "json": dumps(data, None)}])
        self.store.insert_rows("sections", [{"t0": readings.to_seconds(s.get("start"), 0.0),
                                             "t1": readings.to_seconds(s.get("end"), dur), "title": s.get("title", ""),
                                             "summary": s.get("summary", ""), "producer": producer}
                                            for s in data.get("sections", []) or []])
        self.store.insert_rows("moments", [{"t0": readings.to_seconds(m.get("start"), 0.0),
                                            "t1": readings.to_seconds(m.get("end"), 0.0), "title": m.get("title", ""),
                                            "hook": m.get("hook", ""), "why": m.get("why", ""),
                                            "score": m.get("score"), "producer": producer}
                                           for m in data.get("moments", []) or []])

    def stage_hazards(self) -> str:
        tracks = self.store.all("SELECT * FROM text_tracks")
        sents = self.store.all("SELECT * FROM sentences")
        rows = hazards.scan(tracks, sents, self.fps)
        for r in self.store.all("SELECT producer, json FROM readings WHERE scope='video'"):
            rows += hazards.from_reading(json.loads(r["json"]), r["producer"], self.fps, self.info["duration"])
        self.store.replace_rows("hazards", rows)
        kinds = defaultdict(int)
        for r in rows:
            kinds[r["kind"]] += 1
        return dict(kinds).__repr__()

    def stage_marks(self) -> str:
        reps = {int(k): v for k, v in json.loads((self.ws.index / "rep_frames.json").read_text()).items()}
        frames = read_frames(self.src.path, list(reps.values()))
        small_size = (480, 270)
        smalls = [cv2.resize(frames[i], small_size, interpolation=cv2.INTER_AREA) for i in reps.values()]
        sx = self.info["width"] / small_size[0]
        cands = marks.persistent_regions(smalls)
        tracks = self.store.all("SELECT * FROM text_tracks")
        video_readings = [json.loads(r["json"]) for r in self.store.all("SELECT json FROM readings WHERE scope='video'")]
        brand_name = next((d.get("brand") for d in video_readings if d.get("brand")), "") or ""
        brand_tokens = [t for t in re.findall(r"[A-Za-z]{3,}", brand_name) if t.lower() not in ("medical", "affairs", "inc")]
        reading_logo_boxes = []
        for d in video_readings:
            for mk in d.get("marks", []) or []:
                b = mk.get("box_2d") or []
                if len(b) == 4:
                    ymin, xmin, ymax, xmax = b
                    reading_logo_boxes.append((xmin / 1000 * self.info["width"], ymin / 1000 * self.info["height"],
                                               (xmax - xmin) / 1000 * self.info["width"], (ymax - ymin) / 1000 * self.info["height"]))
        logo_track_ids: set[int] = set()
        rows = []
        for c in cands:
            box = [c["x"] * sx, c["y"] * sx, c["w"] * sx, c["h"] * sx]
            pad = 4
            box = [max(0, box[0] - pad), max(0, box[1] - pad), box[2] + 2 * pad, box[3] + 2 * pad]
            inside = [t for t in tracks if layout._center_in((t["x"], t["y"], t["w"], t["h"]), tuple(box))]
            names = sorted({t["text"] for t in inside}, key=len)
            role_set = {t["role"] for t in inside}
            brand_hit = any(ocr.similar(tok, n) >= 0.6 for n in names for tok in brand_tokens)
            reading_hit = any(ocr.iou(tuple(box), lb) > 0.1 or layout._center_in(lb, tuple(box)) for lb in reading_logo_boxes)
            dated = any(re.search(r"\b(19|20)\d\d\b|\|", n) for n in names)
            if any(ocr.JOB_CODE.search(n) for n in names):
                kind = "footer_text"
            elif reading_hit or (brand_hit and not dated):
                kind = "logo"
            elif dated or role_set & {"legal"}:
                kind = "footer_text"
            elif names and all(n.strip().isdigit() for n in names):
                kind = "page_number"
            else:
                kind = "graphic"
            name = (brand_name if kind == "logo" and brand_name else (names[0] if names else "unnamed mark"))
            if kind == "logo":
                for t in inside:
                    logo_track_ids.add(t["id"])
            # template from the median of instances, then the largest clean instance anywhere
            crops = [frames[i][int(box[1]):int(box[1] + box[3]), int(box[0]):int(box[0] + box[2])] for i in reps.values()]
            crops = [cp for cp in crops if cp.size]
            template = np.median(np.stack(crops), axis=0).astype(np.uint8) if crops else None
            best = marks.best_instance(template, frames) if (template is not None and kind == "logo") else None
            present = [s for s, i in reps.items() if crops and np.mean(np.abs(
                frames[i][int(box[1]):int(box[1] + box[3]), int(box[0]):int(box[0] + box[2])].astype(np.int16)
                - template.astype(np.int16))) < 12]
            f0 = min((sh["f0"] for sh in self._shots if sh["id"] in present), default=0)
            f1 = max((sh["f1"] for sh in self._shots if sh["id"] in present), default=0)
            rows.append({"kind": kind, "name": name, "x": box[0], "y": box[1], "w": box[2], "h": box[3],
                         "f0": f0, "f1": f1, "best_frame": best["frame"] if best else None,
                         "best_box": best["box"] if best else None, "persistence": len(present) / max(1, len(reps)),
                         "producer": "marks.persistence", "status": "fact"})
        for r in self.store.all("SELECT producer, json FROM readings WHERE scope='video'"):
            for mk in json.loads(r["json"]).get("marks", []) or []:
                b = mk.get("box_2d") or []
                box = None
                if len(b) == 4:
                    ymin, xmin, ymax, xmax = b
                    box = [xmin / 1000 * self.info["width"], ymin / 1000 * self.info["height"],
                           (xmax - xmin) / 1000 * self.info["width"], (ymax - ymin) / 1000 * self.info["height"]]
                t0 = readings.to_seconds(mk.get("start"), 0.0) or 0.0
                rows.append({"kind": mk.get("kind", "logo"), "name": mk.get("name", ""),
                             "x": box[0] if box else None, "y": box[1] if box else None,
                             "w": box[2] if box else None, "h": box[3] if box else None,
                             "f0": int(t0 * self.fps), "f1": int((readings.to_seconds(mk.get("end"), t0) or t0) * self.fps),
                             "best_frame": None, "best_box": None, "persistence": None,
                             "producer": r["producer"], "status": "claim"})
        self.store.replace_rows("marks", rows)
        # text inside a logo is part of the mark, not copy: it is not regulated text
        for tid in logo_track_ids:
            self.store.connect().execute("UPDATE text_tracks SET role='logo_text', regulated=0 WHERE id=?", (tid,))
        brand_low = [b.lower() for b in brand_tokens]
        for t in tracks:
            if t["role"] in ("body", "label") and brand_low and any(ocr.similar(b, t["text"].lower()) >= 0.6 for b in brand_low):
                self.store.connect().execute("UPDATE text_tracks SET role='logo_text', regulated=0 WHERE id=?", (t["id"],))
        self.store.connect().commit()
        return f"{len(rows)} marks ({sum(1 for r in rows if r['producer'] == 'marks.persistence')} from persistence)"

    def stage_reading_text(self) -> str:
        sents = self.store.all("SELECT * FROM sentences ORDER BY id")
        transcript = "\n".join(f"s{s['id']:03d} {s['t0']:.2f}-{s['t1']:.2f} {s['text']}" for s in sents)
        tracks = self.store.all("SELECT * FROM text_tracks ORDER BY f0, y")
        lines = []
        for s in self._shots:
            texts = [t["text"] for t in tracks if t["shot"] == s["id"]]
            lines.append(f"shot {s['id']} {s['t0']:.2f}-{s['t1']:.2f}: " + " | ".join(texts)[:900])
        reps = {int(k): v for k, v in json.loads((self.ws.index / "rep_frames.json").read_text()).items()}
        frames = read_frames(self.src.path, list(reps.values()), size=(640, 360))
        cells = [(f"shot {sid} {self._shots[sid]['t0']:.1f}s", frames[i]) for sid, i in reps.items()]
        sheets = [jpeg_bytes(contact_sheet(cells[k:k + 12], cols=4, cell_width=480)) for k in range(0, len(cells), 12)]
        data = readings.text_reading(self.gw, transcript, "\n".join(lines), sheets)
        self._store_reading(data, producer=f"reading.text:{data.get('_model', '?')}", scope="video")
        return f"{len(data.get('moments', []))} moments"

    def stage_disagreements(self) -> str:
        rs = [json.loads(r["json"]) for r in self.store.all("SELECT json FROM readings WHERE scope='video' ORDER BY id")]
        self.store.connect().execute("DELETE FROM observations WHERE kind='reading.disagreement'")
        self.store.connect().commit()
        if len(rs) < 2:
            return "one reading only"
        notes = readings.describe_disagreements(rs[0], rs[1])
        for note in notes:
            self.store.add_observation(media_id=self.src.media_id, media_rev=self.src.rev, kind="reading.disagreement",
                                       f0=None, f1=None, value={"note": note}, text=note, producer="index.compare",
                                       status="fact")
        return f"{len(notes)} disagreements"


def reuse_index(ws: Workspace, src: SourceRef, progress: Callable[[str], None] | None = None) -> bool:
    """Seed an empty index from another request's complete index of the same media (same sha256 and
    index version). Every stage then reports cached; nothing about the index differs between requests."""
    if (ws.index / "index.sqlite").exists() or force_fresh():
        return False
    from vwb.config import RUNS_DIR
    candidates = []
    for req_json in Path(RUNS_DIR).glob("*/request.json"):
        other = req_json.parent
        if other.resolve() == ws.root.resolve() or not (other / "index" / "index.sqlite").exists():
            continue
        try:
            data = json.loads(req_json.read_text())
        except json.JSONDecodeError:
            continue
        if not any(s.get("sha256") == src.sha256 for s in data.get("sources", [])):
            continue
        st = IndexStore(other / "index" / "index.sqlite")
        if not st.stage_done("disagreements", INDEX_VERSION, {"v": 1}):
            continue
        n_stages = st.one("SELECT COUNT(*) AS n FROM stages")["n"]       # the most complete index wins, then the newest
        candidates.append((n_stages, (other / "index" / "index.sqlite").stat().st_mtime, other))
    if not candidates:
        return False
    _, _, other = max(candidates)
    shutil.copytree(other / "index", ws.index, dirs_exist_ok=True)
    (progress or (lambda m: log.info("index: %s", m)))(f"reused the index of {other.name} (same media, same version)")
    return True


def force_fresh() -> bool:
    return os.environ.get("VWB_FRESH_INDEX", "") == "1"


def build_index(ws: Workspace, media_id: str | None = None, force: bool = False,
                progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    req = ws.load()
    src = req.source(media_id)
    if not force:
        reuse_index(ws, src, progress)
    return IndexBuilder(ws, src, force=force, progress=progress).run()


def resize_for_sheet(rgb: np.ndarray, width: int = 480) -> np.ndarray:
    return resize_to_width(rgb, width)
