"""The dossier: the compact view of the edit index that an agent reads first (spec "Pack").

It summarises; the index holds the detail. Every line points back to ids an agent can
query (shots, sentences, text tracks, hazards), so nothing here is a dead end.
"""

from __future__ import annotations

import json
from typing import Any

from vwb.index.store import IndexStore
from vwb.util.timecode import fmt


def _t(a: float, b: float) -> str:
    return f"{fmt(a)}–{fmt(b)}"


def build(store: IndexStore, source_label: str) -> str:
    m = store.media()
    info = json.loads(m["info"])
    lines: list[str] = []
    add = lines.append
    add(f"# Edit index dossier: {source_label}")
    add("")
    add(f"Source: {info['width']}x{info['height']} at {info['fps']:.3f} fps, {info['duration']:.2f} s, "
        f"{info['n_frames']} frames, {info['vcodec']} {info['pix_fmt']}, colour {info.get('color_space')}, "
        f"audio {'yes' if info['has_audio'] else 'no'} ({info.get('audio_codec')}). "
        f"Keyframes: {len(info.get('keyframes', []))}. Frames are the unit of truth; intervals are half-open [f0, f1).")
    add("")

    readings = store.all("SELECT producer, model, json FROM readings WHERE scope='video' ORDER BY id")
    for r in readings:
        data = json.loads(r["json"])
        add(f"## Reading {r['producer']} ({r['model']}) — a claim, not evidence")
        add(f"Summary: {data.get('summary', '').strip()}")
        if data.get("audience"):
            add(f"Audience (as read): {data['audience']}")
        if data.get("brand"):
            add(f"Brand: {data['brand']}. Content type: {data.get('content_type', '')}")
        style = data.get("style") or {}
        if style:
            add(f"Style: primary {style.get('primary_color')}, accent {style.get('accent_color')}, type: {style.get('font_feel')}")
        add("")
    dis = store.all("SELECT text FROM observations WHERE kind='reading.disagreement'")
    if dis:
        add("Readings disagree on: " + "; ".join(d["text"] for d in dis))
        add("")

    add("## Hazards for a regulated reviewer")
    hz = store.all("SELECT * FROM hazards ORDER BY t0")
    usage = [h for h in hz if h["kind"].startswith("usage_terms") or h["kind"] == "audience"]
    for h in usage:
        add(f"- [{_t(h['t0'], h['t1'])}] {h['kind']} ({h['producer']}): \"{(h['quote'] or h['text'])[:400]}\"")
    for kind in ("isi", "job_code"):
        rows = [h for h in hz if h["kind"] == kind]
        if rows:
            texts = sorted({(h["quote"] or h["text"]) for h in rows})
            add(f"- {kind}: {len(rows)} occurrence(s): " + " | ".join(f'"{t[:80]}"' for t in texts[:4]))
    n_claim_screen = sum(1 for h in hz if h["kind"] == "claim" and h["producer"].startswith("rules"))
    n_claim_spoken = sum(1 for h in hz if h["kind"] == "claim.spoken")
    n_refs = sum(1 for h in hz if h["kind"] == "reference")
    add(f"- claims: {n_claim_screen} on-screen text tracks, {n_claim_spoken} spoken sentences; "
        f"references/footnotes: {n_refs} tracks. Query: SELECT * FROM hazards WHERE kind LIKE 'claim%'.")
    model_h = [h for h in hz if h["status"] == "claim" and h["kind"] not in ("usage_terms", "audience")]
    for h in model_h[:12]:
        add(f"- [{_t(h['t0'], h['t1'])}] {h['kind']} (reading): \"{(h['quote'] or '')[:160]}\"")
    add("")

    marks = store.all("SELECT * FROM marks ORDER BY persistence DESC")
    if marks:
        add("## Marks (logos and page furniture)")
        for mk in marks:
            best = json.loads(mk["best_box"]) if mk["best_box"] else None
            box = (f"[{mk['x']:.0f},{mk['y']:.0f},{mk['w']:.0f},{mk['h']:.0f}]" if mk["x"] is not None else "unknown")
            pers = f", persistence {mk['persistence']:.2f}" if mk["persistence"] is not None else ""
            add(f"- mark {mk['id']} {mk['kind']} \"{mk['name']}\" at box {box}{pers}, producer {mk['producer']}"
                + (f"; largest clean instance at frame {mk['best_frame']} box {[round(v) for v in best]}" if best else ""))
        add("")

    secs = store.all("SELECT * FROM sections ORDER BY producer, t0")
    if secs:
        add("## Sections")
        for s in secs:
            add(f"- [{_t(s['t0'], s['t1'])}] ({s['producer'].split(':')[0]}) {s['title']}")
        add("")

    add("## Shots")
    add("| shot | time | kind | title | text tracks | face |")
    add("|---|---|---|---|---|---|")
    for s in store.all("SELECT * FROM shots ORDER BY id"):
        add(f"| {s['id']} | {_t(s['t0'], s['t1'])} | {s['kind'] or ''} | {(s['title'] or '')[:90]} | {s['n_text']} | "
            f"{'yes' if s['has_face'] else ''} |")
    add("")

    add("## Transcript (two readers; disagreements marked)")
    for s in store.all("SELECT * FROM sentences ORDER BY id"):
        mark = "" if s["agree"] >= 0.999 else f"  [readers agree on {s['agree']:.0%} of words]"
        add(f"s{s['id']:03d} [{_t(s['t0'], s['t1'])}] shot {s['shot']}: {s['text']}{mark}")
        if s["agree"] < 0.999:
            dis = store.all("SELECT text, alt FROM words WHERE sentence=? AND agree=0", (s["id"],))
            pairs = [f"\"{d['text']}\"→\"{d['alt']}\"" for d in dis if d["alt"] is not None][:6]
            if pairs:
                add("      reader 2 heard: " + ", ".join(pairs))
    add("")

    moments = store.all("SELECT * FROM moments ORDER BY score DESC, t0")
    if moments:
        add("## Moments proposed by the readings (claims; validate before use)")
        for mo in moments[:24]:
            add(f"- [{_t(mo['t0'], mo['t1'])}] {mo['score'] or ''} ({mo['producer'].split(':')[0]}) {mo['title']} — hook: {mo['hook'] or ''}")
        add("")

    add("## Query the index")
    add("Tables: shots, sentences, words, text_tracks (role, regulated, agree), faces, persons, saliency, blocks, "
        "silences, sections, moments, hazards, marks, readings, observations; view: envelope. "
        "Use index_query for SQL, index_search for text, index_transcript for timed words.")
    return "\n".join(lines)
