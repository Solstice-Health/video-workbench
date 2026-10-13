"""Cue sheet and delivery note, written from the canvas, the receipt and the ledger only.

The delivery note never contains model prose: every line comes from a record.
"""

from __future__ import annotations

import json
from typing import Any

from vwb.canvas.anim import is_keyed
from vwb.canvas.model import Canvas, CaptionsLayer, ClipLayer, ImageLayer, TextLayer, VideoLayer
from vwb.index.store import IndexStore
from vwb.util.timecode import fmt
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry

SEV_ORDER = {"BLOCK": 0, "HOLD": 1, "WARN": 2, "INFO": 3}


def cue_sheet(ws: Workspace, canvas: Canvas, store: IndexStore, receipt: dict[str, Any]) -> dict[str, Any]:
    fps = canvas.fps
    shots = store.all("SELECT * FROM shots ORDER BY id")
    rows, out_f = [], 0
    for span in canvas.time_map:
        n = span.src_out - span.src_in
        crossed = [{"shot": s["id"], "kind": s["kind"], "title": (s["title"] or "")[:90]}
                   for s in shots if s["f0"] < span.src_out and s["f1"] > span.src_in]
        rows.append({"out_in": fmt(out_f / fps), "out_out": fmt((out_f + n) / fps), "media": span.media,
                     "src_in": fmt(span.src_in / fps), "src_out": fmt(span.src_out / fps),
                     "src_frames": [span.src_in, span.src_out], "shots": crossed})
        out_f += n
    camera = {}
    for l in canvas.layers:
        if isinstance(l, VideoLayer) and l.crop != "full" and is_keyed(l.crop):
            camera[l.id] = [{"at": fmt(k.f / fps), "window": [round(v) for v in k.v], "ease": k.ease} for k in l.crop]
    copy = canvas.copy_by_id()
    texts = [{"layer": l.id, "copy": l.copy_id, "text": copy[l.copy_id].text,
              "span": [fmt((l.span or [0, canvas.length])[0] / fps), fmt((l.span or [0, canvas.length])[1] / fps)]}
             for l in canvas.layers if isinstance(l, TextLayer)]
    captions = [{"copy": pg["copy_id"], "in": fmt(pg["from"] / fps), "out": fmt(pg["to"] / fps)}
                for l in canvas.layers if isinstance(l, CaptionsLayer) for pg in (l.pages or [])]
    reg = Registry(ws.assets)
    assets = []
    for l in canvas.layers:
        if isinstance(l, (ImageLayer, ClipLayer)):
            row = reg.get(l.asset)
            fit = json.loads(row["fit"])
            assets.append({"layer": l.id, "ref": l.asset, "kind": row["kind"], "prov": row["prov"], "made_by": row["receipt"],
                           "model": fit.get("model"), "prompt": fit.get("prompt"), "sha256": row["sha256"],
                           "derived_from": json.loads(row["derived_from"])})
    return {"output": canvas.output, "version": canvas.version, "file_sha256": receipt["sha256"],
            "duration": fmt(canvas.length / fps), "frames": canvas.length, "segments": rows, "camera": camera,
            "text_layers": texts, "captions": captions, "assets": assets,
            "layers": [{"id": l.id, "type": l.type, "role": l.role} for l in canvas.layers]}


def delivery_note(ws: Workspace, canvas: Canvas, receipt: dict[str, Any], result: dict[str, Any],
                  cue: dict[str, Any]) -> str:
    req = ws.load()
    L: list[str] = []
    add = L.append
    add(f"# Delivery note: {canvas.output} v{canvas.version}")
    add("")
    add(f"Request `{req.id}`: \"{req.message}\"")
    add(f"File `{ws.rel(receipt['file'])}` (sha256 `{receipt['sha256'][:16]}…`), {canvas.format['width']}x{canvas.format['height']}, "
        f"{canvas.fps} fps, {receipt['seconds']:.2f} s, H.264 + AAC, loudness target "
        f"{canvas.audio.loudness_lufs} LUFS.")
    add("")
    add(f"**Triage: {result['triage']}.** Attested by re-render: {'yes' if result['attested'] else 'no'}. "
        "This is not PRC or MLR approval. No person has accepted this version.")
    add("")
    add("## Source")
    for seg in cue["segments"]:
        shots = ", ".join(f"{s['shot']} ({s['kind']})" for s in seg["shots"])
        add(f"- out {seg['out_in']}–{seg['out_out']} ← {seg['media']} {seg['src_in']}–{seg['src_out']} "
            f"(frames {seg['src_frames'][0]}–{seg['src_frames'][1]}); shots {shots}")
    add("")
    open_flags = sorted((f for f in result["flags"] if f["status"] == "open"), key=lambda f: (SEV_ORDER[f["severity"]], f["kind"]))
    add(f"## Flags ({len(open_flags)} open)")
    add("| severity | kind | what | id |")
    add("|---|---|---|---|")
    for f in open_flags:
        add(f"| {f['severity']} | {f['kind']} | {f['summary']} | `{f['id']}` |")
    add("")
    add("## What this version is")
    if canvas.intent:
        add(f"Intent (author): {canvas.intent}")
    for l in canvas.layers:
        add(f"- layer `{l.id}` ({l.type}, {l.role})" + (f": {l.note}" if l.note else ""))
    add("")
    if cue.get("assets"):
        add("## Images and clips (provenance)")
        add("| layer | asset | provenance | made by | model | prompt |")
        add("|---|---|---|---|---|---|")
        for x in cue["assets"]:
            prompt = (x.get("prompt") or "").replace("|", "/")[:160]
            add(f"| {x['layer']} | `{x['ref']}` | {x['prov']} | {x['made_by']} | {x.get('model') or ''} | {prompt} |")
        add("")
    add("## Typeset copy")
    add("| id | role | text | source | readers agree | regulated | status |")
    add("|---|---|---|---|---|---|---|")
    for c in canvas.copy_deck:
        add(f"| {c.id} | {c.role} | {c.text.replace('|', '/')} | {c.source.get('kind')} | {c.agree} | {c.regulated} | {c.status} |")
    add("")
    add("## Checks (all unqualified in v0)")
    for f in result["findings"]:
        if f["tier"] in ("T0", "T1"):
            add(f"- {f['check']}: **{f['status']}**: {f['summary']}")
    add("")
    add("To accept: every WARN-or-higher flag acknowledged by id, BLOCK flags waived by a senior pair or RA, "
        "then a signed `/accept`.")
    return "\n".join(L)
