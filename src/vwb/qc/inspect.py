"""T1 checks: declared (canvas + index) and observed (the delivered file).

Declared checks replay the canvas transform track against the index: which source text
and faces the window keeps whole, cuts, or drops, and how large text ends up. Observed
checks read the delivered file back: two OCR engines on every typeset string, a re-render
of sampled frames from the canvas (attestation), and a transcript of the output audio.
"""

from __future__ import annotations

import difflib
import re
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from vwb.canvas.anim import active, all_values, value_at
from vwb.canvas.model import Canvas, CaptionsLayer, ImageLayer, TextLayer, VideoLayer
from vwb.index.asr import word_key
from vwb.index.ocr import Reader2, agree_key, similar
from vwb.index.paths import SourceIndexes, store_for
from vwb.index.store import IndexStore
from vwb.media.decode import read_frames
from vwb.qc.findings import Finding
from vwb.reframe.solver import TimeMap
from vwb.util.procs import ffmpeg

CLAIM_ROLES = {"claim", "isi"}
HOLD_ROLES = {"footnote", "reference", "job_code", "usage_terms", "legal"}


def _vis(win, box) -> float:
    wx, wy, ww, wh = win
    ix = max(0.0, min(box[0] + box[2], wx + ww) - max(box[0], wx))
    iy = max(0.0, min(box[1] + box[3], wy + wh) - max(box[1], wy))
    return ix * iy / max(1.0, box[2] * box[3])


def _vis_masked(win, box, shape: str) -> float:
    """Visible share of a source box inside a window, honouring circle and ellipse masks."""
    if shape not in ("circle", "ellipse"):
        return _vis(win, box)
    wx, wy, ww, wh = win
    if shape == "circle":
        r = min(ww, wh) / 2
        rx = ry = r
    else:
        rx, ry = ww / 2, wh / 2
    cx, cy = wx + ww / 2, wy + wh / 2
    n, inside = 0, 0
    for i in range(6):
        for j in range(6):
            px = box[0] + box[2] * (i + 0.5) / 6
            py = box[1] + box[3] * (j + 0.5) / 6
            n += 1
            inside += ((px - cx) / rx) ** 2 + ((py - cy) / ry) ** 2 <= 1.0
    return inside / n


def content_layers(canvas: Canvas) -> list[VideoLayer]:
    """Video layers that show the source to be read (not blurred backdrops)."""
    return [l for l in canvas.layers if isinstance(l, VideoLayer) and l.role != "background" and l.effects.blur < 4]


def typeset_tracks(canvas: Canvas) -> tuple[set[tuple[str, int]], list[tuple[str | None, str]]]:
    """Source text tracks the output carries as typeset copy (by track id, and the copy texts)."""
    ids: set[tuple[str, int]] = set()
    texts: list[tuple[str | None, str]] = []
    copy = canvas.copy_by_id()
    for l in canvas.layers:
        if isinstance(l, TextLayer) and l.copy_id in copy:
            c = copy[l.copy_id]
            media = c.source.get("media")
            ids.update((media, int(t)) for t in c.source.get("tracks", []) or [])
            texts.append((media or ("unknown" if c.source.get("kind") in {"ocr", "asr"} else None), c.text))
    return ids, texts


def source_pixel_strips(ws: Any, canvas: Canvas) -> list[tuple[str | None, int, list[float]]]:
    """Image layers that show unaltered source pixels (matte or recovered cut-outs, not redrawn ones):
    (source frame, box in source pixels) for each, from the asset's registry row."""
    if ws is None:
        return []
    import json

    from vwb.workspace.registry import Registry
    reg = Registry(ws.assets)
    out = []
    for l in canvas.layers:
        if not isinstance(l, ImageLayer):
            continue
        try:
            row = reg.get(l.asset)
        except KeyError:
            continue
        if row["prov"] not in ("original_pixel", "fused", "segmented"):
            continue
        fit = json.loads(row["fit"])
        placed = fit.get("placed_in_source")
        src = [d for d in json.loads(row["derived_from"]) if d.get("kind") == "source_frame"]
        if placed and src:
            x, y, w, h = (float(v) for v in placed)
            if fit.get("placed_frame") != "source" and src[0].get("crop"):   # rows written before placements
                x, y = x + src[0]["crop"][0], y + src[0]["crop"][1]          # were made source-relative
            out.append((src[0].get("media"), int(src[0]["frame"]), [x, y, w, h]))
    return out


def image_scales(ws: Any, canvas: Canvas) -> list[dict[str, Any]]:
    """For every image layer that shows source pixels: its asset, the source box, and the smallest and
    largest scale it is shown at (fit into its box, over its keyframes)."""
    if ws is None:
        return []
    import json

    from vwb.workspace.registry import Registry
    reg = Registry(ws.assets)
    out = []
    for l in canvas.layers:
        if not isinstance(l, ImageLayer):
            continue
        try:
            row = reg.get(l.asset)
        except KeyError:
            continue
        if row["prov"] not in ("original_pixel", "fused", "segmented"):
            continue
        fit = json.loads(row["fit"])
        nw, nh = (fit.get("native_size") or [0, 0])[:2]
        if not nw or not nh:
            continue
        scales = [(min if l.fit == "contain" else max)(b[2] / nw, b[3] / nh) for b in all_values(l.box)]
        src = [d for d in json.loads(row["derived_from"]) if d.get("kind") == "source_frame"]
        placed = fit.get("placed_in_source")
        if placed and src and fit.get("placed_frame") != "source" and src[0].get("crop"):
            placed = [placed[0] + src[0]["crop"][0], placed[1] + src[0]["crop"][1], placed[2], placed[3]]
        out.append({"layer": l.id, "asset": l.asset, "min_scale": min(scales), "max_scale": max(scales),
                    "frame": int(src[0]["frame"]) if src else None, "media": src[0].get("media") if src else None, "placed": placed})
    return out


def declared(canvas: Canvas, store: SourceIndexes | IndexStore, profile: dict[str, Any], step: int = 3, ws: Any = None) -> list[Finding]:
    """Replay every source window over the index (kept, cut, size, faces, motion) and account for the
    regulated text of the kept span: shown in a window, carried in a strip of source pixels, rebuilt as
    typeset copy, or dropped."""
    out: list[Finding] = []
    unknown = [c.id for c in canvas.copy_deck if c.source.get("kind") in {"ocr", "asr"} and not c.source.get("media")]
    if unknown:
        out.append(Finding("t1.source_identity", "cannot_tell", "CHECK-UNQUALIFIED",
                           "Source-derived copy is missing its media reference; remint it with an explicit media_id.",
                           {"copies": unknown}, tier="T1"))
    layers = content_layers(canvas)
    tm = TimeMap([(s.src_in, s.src_out) for s in canvas.time_map])
    refs = list(dict.fromkeys(s.media for s in canvas.time_map))
    scales = image_scales(ws, canvas)
    for sc in scales:
        if sc["media"] and sc["media"] not in refs:
            refs.append(sc["media"])
    stores = {ref: store_for(store, ref) for ref in refs}
    media = {ref: st.media() for ref, st in stores.items()}
    tracks = [{**t, "media": ref, "_key": (ref, t["id"])} for ref, st in stores.items()
              for t in st.all("SELECT * FROM text_tracks") if t["role"] != "logo_text"]
    faces = [{**fc, "media": ref} for ref, st in stores.items() for fc in st.all("SELECT * FROM faces ORDER BY f")]
    timeline, offset = [], 0
    for span in canvas.time_map:
        timeline.append((offset, offset + span.src_out - span.src_in, span))
        offset += span.src_out - span.src_in
    seen: dict[tuple[str, int], int] = {}
    vis_n: dict[tuple[str, int], int] = {}
    cut_n: dict[tuple[str, int], int] = {}
    small: dict[tuple[str, int], float] = {}
    face_cut = face_n = 0
    max_scale = 0.0
    motion = {l.id: 0.0 for l in layers}
    prev: dict[str, tuple] = {}
    for f in range(0, tm.length, step):
        start, _, span = next(row for row in timeline if row[0] <= f < row[1])
        ref, sf = span.media, span.src_in + f - start
        sw, sh = float(media[ref]["width"]), float(media[ref]["height"])
        live = [l for l in layers if l.source == ref and active(l.span, f) and float(value_at(l.opacity, f)) > 0.05]
        wins = []
        for l in live:
            win = (0.0, 0.0, sw, sh) if l.crop == "full" else tuple(float(v) for v in value_at(l.crop, f))
            box = value_at(l.box, f)
            scale = box[2] / win[2]
            max_scale = max(max_scale, scale)
            if l.id in prev:
                p = prev[l.id]
                motion[l.id] += abs(win[0] - p[0]) / win[2] + abs(win[1] - p[1]) / win[3] + abs(win[2] - p[2]) / win[2]
            prev[l.id] = win
            wins.append((l, win, scale))
        for t in tracks:
            if t["media"] != ref or not (t["f0"] <= sf < t["f1"]):
                continue
            tb = (t["x"], t["y"], t["w"], t["h"])
            seen[t["_key"]] = seen.get(t["_key"], 0) + 1
            best = 0.0
            for l, win, scale in wins:
                v = _vis_masked(win, tb, l.mask.shape)
                best = max(best, v)
                if 0.02 < v < 0.96:
                    cut_n[t["_key"]] = cut_n.get(t["_key"], 0) + 1
                if v >= 0.96 and t["cap_h"]:
                    small[t["_key"]] = min(small.get(t["_key"], 1e9), t["cap_h"] * scale)
            if best > 0.02:
                vis_n[t["_key"]] = vis_n.get(t["_key"], 0) + 1
        for fc in faces:
            if fc["media"] == ref and abs(fc["f"] - sf) <= 2:
                fb = (fc["x"], fc["y"], fc["w"], fc["h"])
                for l, win, _ in wins:
                    v = _vis_masked(win, fb, l.mask.shape)
                    if v > 0.0:
                        face_n += 1
                        if v < 0.7:
                            face_cut += 1
    by_id = {t["_key"]: t for t in tracks}
    fps = canvas.fps
    clipped = [by_id[i] for i, n in cut_n.items() if n / max(1, vis_n.get(i, 1)) > 0.25]
    for group, level in ((lambda t: t["role"] in CLAIM_ROLES, "HOLD"), (lambda t: t["role"] in HOLD_ROLES, "HOLD"),
                         (lambda t: t["role"] not in CLAIM_ROLES | HOLD_ROLES, "WARN")):
        items = [t for t in clipped if group(t)]
        if items:
            out.append(Finding("t1.text_clipped", "fail", "TEXT-CLIPPED",
                               f"{len(items)} {'regulated ' if level == 'HOLD' else ''}text line(s) cut by a window edge",
                               {"tracks": [{"id": t["id"], "media": t["media"], "role": t["role"], "text": t["text"][:140]} for t in items[:20]]},
                               level=level, tier="T1"))
    dropped = [by_id[i] for i, n in seen.items() if vis_n.get(i, 0) == 0 and n * step >= fps * 0.5
               and (by_id[i]["regulated"] or by_id[i]["role"] in CLAIM_ROLES | HOLD_ROLES)]
    typeset_ids, typeset_texts = typeset_tracks(canvas)
    typeset_keys = [(ref, agree_key(text)) for ref, text in typeset_texts]
    strips = source_pixel_strips(ws, canvas)

    def in_strip(t: dict[str, Any]) -> bool:
        tb = (t["x"], t["y"], t["w"], t["h"])
        return any(ref == t["media"] and t["f0"] <= f < t["f1"] and _vis_masked(tuple(box), tb, "rect") >= 0.9
                   for ref, f, box in strips)

    dropped = [t for t in dropped if t["_key"] not in typeset_ids and not in_strip(t)
               and not any((ref is None or ref == t["media"]) and
                           (agree_key(t["text"]) in k or similar(agree_key(t["text"]), k) > 0.85) for ref, k in typeset_keys)]
    if dropped:
        roles = sorted({t["role"] for t in dropped})
        out.append(Finding("t1.text_dropped", "fail", "TEXT-DROPPED",
                           f"{len(dropped)} regulated text line(s) in the kept span appear neither in a source window nor "
                           f"as typeset copy ({', '.join(roles)})",
                           {"tracks": [{"id": t["id"], "media": t["media"], "role": t["role"], "text": t["text"][:160]} for t in dropped[:40]]},
                           level="HOLD", tier="T1"))
    floor = profile["legibility"]["min_cap_height_px"]
    shown = [i for i in small if by_id[i]["role"] in ("body", "claim", "title")]
    med = float(np.median([small[i] for i in shown])) if shown else None
    tiny = sorted((round(small[i], 1), by_id[i]["text"][:60]) for i in shown if small[i] < floor)
    if med is not None and med < floor:
        out.append(Finding("t1.legibility", "fail", "LEGIBILITY-BELOW-FLOOR",
                           f"median on-screen text cap height {med:.1f}px is under the {floor}px floor (UNCALIBRATED)",
                           {"median_px": round(med, 1), "smallest": tiny[:10]}, tier="T1"))
    else:
        out.append(Finding("t1.legibility", "pass", summary=f"median on-screen text cap height {med:.1f}px" if med else
                           "no source text shown", evidence={"median_px": med, "below_floor": len(tiny)}, tier="T1"))
    fr = face_cut / max(1, face_n)
    out.append(Finding("t1.faces", "fail" if fr > 0.05 else "pass", "FACE-CUT" if fr > 0.05 else None,
                       f"faces cut in {fr:.0%} of face samples" if face_n else "no faces shown",
                       {"cut": face_cut, "samples": face_n}, tier="T1"))
    strip_text = []
    for sc in scales:
        if sc["frame"] is None or not sc["placed"]:
            continue
        for t in tracks:
            if t["media"] == sc["media"] and t["f0"] <= sc["frame"] < t["f1"] and t["cap_h"] and \
                    _vis_masked(tuple(sc["placed"]), (t["x"], t["y"], t["w"], t["h"]), "rect") >= 0.9:
                strip_text.append((round(t["cap_h"] * sc["min_scale"], 1), sc["layer"], t["text"][:60], t["role"]))
    fine = profile["legibility"].get("min_fineprint_cap_px", floor)
    tiny_strips = sorted(x for x in strip_text if x[0] < (fine if x[3] in ("footnote", "reference") else floor))
    if tiny_strips:
        out.append(Finding("t1.legibility_strips", "fail", "LEGIBILITY-BELOW-FLOOR",
                           f"{len(tiny_strips)} line(s) of source text shown as image pixels under the floor ({floor}px, "
                           f"{fine}px for fine print; smallest {tiny_strips[0][0]}px in {tiny_strips[0][1]}); retype them as copy",
                           {"lines": [{"cap_px": c, "layer": la, "text": tx, "role": r} for c, la, tx, r in tiny_strips[:20]]},
                           tier="T1"))
    up = [sc for sc in scales if sc["max_scale"] > 1.25]
    if max_scale > 1.25 or up:
        worst = max([max_scale] + [sc["max_scale"] for sc in up])
        out.append(Finding("t1.upscale", "fail", "UPSCALED", f"source pixels shown at up to {worst:.2f}x their native size"
                           + (f" ({len(up)} image layer(s): recreate or trace them instead)" if up else ""),
                           {"max_scale": round(worst, 3), "windows_max": round(max_scale, 3),
                            "images": [{"layer": sc["layer"], "asset": sc["asset"], "max_scale": round(sc["max_scale"], 2)}
                                       for sc in sorted(up, key=lambda x: -x["max_scale"])[:20]]}, tier="T1"))
    dur = max(1e-6, tm.length / fps)
    busiest = max(motion.values()) / dur if motion else 0.0
    out.append(Finding("t1.camera", "fail" if busiest > 0.6 else "pass", "V4-CAMERA" if busiest > 0.6 else None,
                       f"busiest window moves {busiest:.2f} window-widths per second",
                       {"motion_per_s": {k: round(v / dur, 3) for k, v in motion.items()}}, tier="T1"))
    W, H = canvas.size
    sz = profile["safe_zone"]
    safe = (sz["left"], sz["top"], W - sz["right"], H - sz["bottom"])
    outside = []
    for l in canvas.layers:
        if isinstance(l, (TextLayer, CaptionsLayer)) or (isinstance(l, ImageLayer) and l.role == "brand"):
            for b in all_values(l.box):
                if b[0] < safe[0] - 1 or b[1] < safe[1] - 1 or b[0] + b[2] > safe[2] + 1 or b[1] + b[3] > safe[3] + 1:
                    outside.append(l.id)
                    break
    out.append(Finding("t1.safe_area", "fail" if outside else "pass", "SAFE-AREA" if outside else None,
                       f"outside the platform safe zone: {outside}" if outside else "every text, caption and brand layer is inside the safe zone",
                       {"outside": outside, "safe": list(safe)}, tier="T1"))
    return out


def boundaries(canvas: Canvas, store: SourceIndexes | IndexStore) -> list[Finding]:
    bad = []
    for s in canvas.time_map:
        st = store_for(store, s.media)
        fps = float(st.media()["fps"])
        words = st.all("SELECT * FROM words ORDER BY t0")
        for edge, f in (("in", s.src_in), ("out", s.src_out)):
            t = f / fps
            w = next((w for w in words if w["t0"] + 0.02 < t < w["t1"] - 0.02), None)
            if w:
                bad.append({"media": s.media, "edge": edge, "frame": f, "t": round(t, 3), "word": w["text"]})
    return [Finding("t1.boundary_words", "fail" if bad else "pass", "BOUNDARY-WORD" if bad else None,
                    f"{len(bad)} cut(s) inside a word" if bad else "every cut sits in a speech gap", {"cuts": bad}, tier="T1")]


# ---------------------------------------------------------------- observed

def _strip_markup(text: str) -> str:
    return re.sub(r"\^\{([^}]*)\}", r"\1", text)


def _tight(text: str) -> str:
    return re.sub(r"\s+", "", agree_key(_strip_markup(text)))


def readback_probes(canvas: Canvas) -> list[tuple[str, int, list[float], str]]:
    """(copy id, frame, box, expected text) for every typeset string, after its entry animation."""
    copy = canvas.copy_by_id()
    probes = []
    for l in canvas.layers:
        if isinstance(l, TextLayer):
            a = l.span[0] if l.span else 0
            b = l.span[1] if l.span else canvas.length
            f = min(b - 1, a + max(l.animate.frames * 3, 12) + 6)
            probes.append((l.copy_id, f, [float(v) for v in value_at(l.box, f)], copy[l.copy_id].text))
        elif isinstance(l, CaptionsLayer):
            for pg in l.pages or []:
                last_from = max(w["from"] for ln in pg["lines"] for w in ln)
                f = min(pg["to"] - 1, max(last_from + 2, pg["from"] + 8))
                probes.append((pg["copy_id"], f, [float(v) for v in value_at(l.box, f)], copy[pg["copy_id"]].text))
    return probes


def text_readback(canvas: Canvas, render: Path, min_cap_px: float = 26.0) -> list[Finding]:
    """Read every typeset string back from the delivered pixels with two OCR engines."""
    from vwb.index import vision
    probes = readback_probes(canvas)
    if not probes:
        return [Finding("t1.text_readback", "info", summary="no typeset text", tier="T1")]
    frames = read_frames(render, [p[1] for p in probes])
    reader2 = Reader2()
    results, mismatches, unsure, small = [], [], [], []
    W, H = canvas.size
    for cid, f, box, expected in probes:
        rgb = frames.get(f)
        if rgb is None:
            unsure.append(cid)
            continue
        x, y, w, h = (int(round(v)) for v in box)
        crop = rgb[max(0, y - 10):min(H, y + h + 10), max(0, x - 10):min(W, x + w + 10)]
        lines1 = sorted(vision.analyze(crop, faces=False, humans=False, saliency=False).lines,
                        key=lambda ln: (round(ln.box[1] / 20), ln.box[0]))
        r1 = " ".join(ln.text for ln in lines1)
        lines2 = reader2.read_frame(crop)
        r2 = " ".join(ln["text"] for ln in sorted(lines2, key=lambda ln: (round(ln["box"][1] / 20), ln["box"][0])))
        exp = _tight(expected)
        ok1, ok2 = _tight(r1) == exp, _tight(r2) == exp
        status = "pass" if (ok1 or ok2) else ("fail" if _tight(r1) == _tight(r2) and r1 else "cannot_tell")
        cap = float(np.median([ln.box[3] for ln in lines1])) * 0.68 if lines1 else None
        if cap is not None and cap < min_cap_px:
            small.append({"copy": cid, "cap_px": round(cap, 1)})
        results.append({"copy": cid, "frame": f, "expected": expected, "reader1": r1, "reader2": r2, "status": status,
                         "cap_px": round(cap, 1) if cap else None})
        if status == "fail":
            mismatches.append(cid)
        elif status == "cannot_tell":
            unsure.append(cid)
    out = [Finding("t1.text_readback", "fail" if mismatches else "pass", "TEXT-MISMATCH" if mismatches else None,
                   (f"{len(mismatches)} typeset string(s) read back differently by both OCR engines" if mismatches else
                    f"{len(results) - len(unsure)} of {len(results)} typeset strings read back exactly"),
                   {"results": results}, tier="T1")]
    if unsure:
        out.append(Finding("t1.text_readback_unsure", "cannot_tell", "CHECK-UNQUALIFIED",
                           f"{len(unsure)} typeset string(s) could not be confirmed by either OCR engine",
                           {"copies": unsure}, level="WARN", tier="T1"))
    if small:
        out.append(Finding("t1.typeset_size", "fail", "LEGIBILITY-BELOW-FLOOR",
                           f"{len(small)} typeset string(s) render below the {min_cap_px}px cap-height floor", {"small": small},
                           tier="T1"))
    return out


def attest(comp_factory, render: Path, length: int, n: int = 24) -> Finding:
    """Re-render sampled frames from the canvas and compare with the delivered pixels, per 32px tile."""
    frames = sorted({int(length * (k + 0.5) / n) for k in range(n)})
    expected = comp_factory().sample(frames)
    actual = read_frames(render, frames)
    worst, psnrs, bad_tiles = 0.0, [], []
    for f in frames:
        a = cv2.cvtColor(expected[f], cv2.COLOR_RGB2GRAY).astype(np.float32)
        b = cv2.cvtColor(actual[f], cv2.COLOR_RGB2GRAY).astype(np.float32)
        mse = float(np.mean((a - b) ** 2))
        psnrs.append(99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse))
        h, w = a.shape
        d = np.abs(a - b)[: h // 32 * 32, : w // 32 * 32].reshape(h // 32, 32, w // 32, 32).mean(axis=(1, 3))
        floor = max(6.0, 5 * float(np.median(d)) + 2.0)
        frac = float(np.mean(d > floor))
        worst = max(worst, frac)
        if frac > 0.005:
            bad_tiles.append({"frame": f, "tile_fraction": round(frac, 4), "floor": round(floor, 2)})
    ok = worst <= 0.005
    return Finding("t1.attest", "pass" if ok else "fail", None if ok else "ATTEST-MISMATCH",
                   f"re-render of {len(frames)} sampled frames matches the delivered file (min PSNR {min(psnrs):.1f} dB)" if ok
                   else f"{len(bad_tiles)} sampled frame(s) differ from a re-render of the canvas",
                   {"frames": frames, "psnr_min": round(min(psnrs), 2), "psnr_median": round(float(np.median(psnrs)), 2),
                    "bad": bad_tiles}, tier="T1")


def audio_readback(canvas: Canvas, store: SourceIndexes | IndexStore, render: Path) -> list[Finding]:
    """Transcribe the output's audio and compare with the words the plan keeps."""
    if not canvas.time_map:
        return [Finding("t1.audio_readback", "info", summary="No source narration on this authored timeline; source speech comparison does not apply.", tier="T1")]
    from vwb.index.asr import transcribe_reader1
    fps = canvas.fps
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "out.wav"
        ffmpeg("-i", str(render), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav), expect=[wav])
        heard = transcribe_reader1(wav, "")
    from vwb.canvas.compile import output_words
    expected = output_words(store, canvas)
    ek = [word_key(w["text"]) for w in expected if word_key(w["text"])]
    hk = [word_key(w["text"]) for w in heard if word_key(w["text"])]
    ratio = difflib.SequenceMatcher(a=ek, b=hk, autojunk=False).ratio() if ek else 0.0
    if not ek and not hk:
        return [Finding("t1.audio_readback", "pass", summary="No source speech expected or heard.", tier="T1")]
    first_ok = bool(ek) and bool(hk) and ek[0] in hk[:3]
    last_ok = bool(ek) and bool(hk) and ek[-1] in hk[-3:]
    out = [Finding("t1.audio_readback", "pass" if ratio >= 0.85 else "fail", None if ratio >= 0.85 else "VO-DIVERGE",
                   f"output audio says what the plan keeps (word match {ratio:.0%})" if ratio >= 0.85
                   else f"output audio diverges from the plan (word match {ratio:.0%})",
                   {"ratio": round(ratio, 3), "expected_words": len(ek), "heard_words": len(hk),
                    "heard": " ".join(w["text"] for w in heard)[:600]}, tier="T1")]
    edge_bad = [n for n, ok in (("first", first_ok), ("last", last_ok)) if not ok]
    out.append(Finding("t1.edge_words", "fail" if edge_bad else "pass", "BOUNDARY-WORD" if edge_bad else None,
                       f"the {' and '.join(edge_bad)} word was not heard in the output" if edge_bad else
                       "first and last words are heard in full", {"first": ek[:1], "last": ek[-1:]}, tier="T1"))
    # caption timing against what the output audio actually says
    cap_words = [w for l in canvas.layers if isinstance(l, CaptionsLayer) for pg in (l.pages or []) for ln in pg["lines"] for w in ln]
    sm = difflib.SequenceMatcher(a=[word_key(w["text"]) for w in cap_words], b=[word_key(w["text"]) for w in heard], autojunk=False)
    offsets = []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            offsets.append(cap_words[blk.a + k]["from"] / fps - heard[blk.b + k]["t0"])
    if offsets:
        med = float(np.median(offsets))
        out.append(Finding("t1.caption_sync", "pass" if abs(med) <= 0.15 else "fail", None if abs(med) <= 0.15 else "CAPTION-LAG",
                           f"captions lead the speech by {med * 1000:.0f} ms (median)", {"median_offset_s": round(med, 3),
                                                                                         "matched": len(offsets)}, tier="T1"))
    return out
