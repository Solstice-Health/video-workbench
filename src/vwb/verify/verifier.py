"""Verify one rendered canvas version and build its review package.

Order: T0 lint -> T1 declared -> T1 observed (OCR read-back, attestation, ASR read-back)
-> standing rules -> flags (code-set severity) -> triage -> cue sheet, delivery note,
manifest. A model never decides a check here; models only nominate elsewhere.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from vwb.canvas.model import Canvas, ClipLayer, ImageLayer, ShapeLayer, TextLayer, VideoLayer
from vwb.canvas.store import CanvasStore
from vwb.config import lever, lever_serves, load_profile
from vwb.index.store import IndexStore
from vwb.qc import inspect, lint
from vwb.qc.findings import Finding
from vwb.render.compositor import Compositor
from vwb.render.pipeline import render_dir
from vwb.util.hashing import sha256_file
from vwb.util.jsonio import read_json, read_jsonl, write_json
from vwb.util.timecode import fmt
from vwb.verify import documents, sidecars
from vwb.workspace import Workspace, flags
from vwb.workspace.ledger import Ledger, sign
from vwb.workspace.registry import Registry

VERIFIER_VERSION = "1.0"


def standing_findings(ws: Workspace, canvas: Canvas, store: IndexStore) -> list[Finding]:
    """Rules that hold for this output whatever the render looks like."""
    out: list[Finding] = []
    hz = store.all("SELECT * FROM hazards WHERE status='fact'")
    usage = [h for h in hz if h["kind"] == "usage_terms"]
    if usage:
        quote = max((h["quote"] or h["text"] for h in usage), key=len)
        out.append(Finding("rule.source_terms", "fail", "SOURCE-TERMS",
                           "The source says it is for US healthcare professionals and must be viewed in its entirety; "
                           "an excerpt for a public channel may break those terms. Needs a Regulatory Affairs ruling.",
                           {"quote": quote, "where": [{"t0": fmt(h["t0"]), "t1": fmt(h["t1"])} for h in usage]},
                           bumps=["regulated"], tier="rule"))
    codes = sorted({h["text"] for h in hz if h["kind"] == "job_code"})
    if codes:
        out.append(Finding("rule.job_code", "fail", "REG-RULE",
                           f"The source carries job code {codes[0]}. A derivative needs its own code from MLR; this output "
                           "shows none.", {"source_codes": codes}, tier="rule"))
    out += asset_findings(ws, canvas)
    copy = canvas.copy_deck
    retyped = [c for c in copy if c.role != "caption" and c.source.get("kind") == "ocr"]
    if retyped:
        ids = {c.id for c in retyped}
        fonts = sorted({f"{l.style.font} {l.style.weight}" for l in canvas.layers
                        if isinstance(l, TextLayer) and l.copy_id in ids})
        out.append(Finding("rule.font", "fail", "NEEDS-SOURCE:font",
                           f"Source text was retyped in {', '.join(fonts) or 'a library font'} (the brand font file is "
                           "not on file).", {"copies": sorted(ids), "fonts": fonts}, tier="rule"))
        out.append(Finding("rule.font_sub", "fail", "FONT-SUBSTITUTED", "Retyped text uses a substitute font.",
                           {"copies": sorted(ids), "fonts": fonts}, tier="rule"))
    by_level: dict[str, list[Any]] = defaultdict(list)
    for c in copy:
        if c.status in ("approved", "confirmed"):
            continue
        level = flags.text_unverified_severity(bool(c.agree), c.regulated)
        by_level[level].append(c)
    for level, items in by_level.items():
        roles = defaultdict(int)
        for c in items:
            roles[c.role] += 1
        what = ", ".join(f"{n} {r}{'s' if n > 1 else ''}" for r, n in roles.items())
        why = {"HOLD": "regulated, readers agree; a person must confirm",
               "BLOCK": "regulated, and the readers disagree or only one reader saw it",
               "WARN": "not regulated"}[level]
        out.append(Finding(f"rule.text_unverified.{level.lower()}", "fail", "TEXT-UNVERIFIED",
                           f"{what} typeset with no approved copy ({why}).",
                           {"copies": [{"id": c.id, "text": c.text, "readers": c.readers} for c in items]},
                           level=level, tier="rule"))
    out.append(Finding("rule.unqualified", "fail", "CHECK-UNQUALIFIED",
                       "No check is qualified on seeded defects yet (v0), so no output can be GREEN.", {}, tier="rule"))
    vendors = sorted({(r.get("provider"), r.get("model")) for r in read_jsonl(ws.ledger / "tools.jsonl")
                      if r.get("provider") and r.get("ok")})
    if vendors:
        out.append(Finding("rule.egress", "info", "EGRESS",
                           "Source frames, audio or text went to: " + ", ".join(f"{p}/{m}" for p, m in vendors),
                           {"vendors": [list(v) for v in vendors]}, tier="rule"))
    req = ws.load()
    for d in (req.spec.assumed_defaults if req.spec else []):
        out.append(Finding("rule.assumed_default", "info", "ASSUMED-DEFAULT", f"{d.item}: {d.value} ({d.why})",
                           {"item": d.item, "value": d.value}, tier="rule"))
    return out


def _from_source(ws: Workspace, lineage: list[dict[str, Any]], depth: int = 0) -> bool:
    """Does an asset descend from the source (a source frame, or an asset that does)?"""
    reg = Registry(ws.assets)
    for d in lineage:
        if d.get("kind") == "source_frame":
            return True
        if d.get("kind") == "asset" and d.get("ref") and depth < 6:
            try:
                row = reg.get(d["ref"])
            except KeyError:
                continue
            if row["receipt"].startswith("asset_lab") or _from_source(ws, json.loads(row["derived_from"]), depth + 1):
                return True
    return False


OP_FLAG = {"extract": "split", "inpaint": "fill", "outpaint": "extend", "omni": "animate", "trace": "trace"}


def _maker(fit: dict[str, Any], rec: dict[str, Any]) -> str:
    """Who made an asset, for a flag's summary: the model, else the tool that recorded it (tracing has no model)."""
    tools = {"asset.trace": "VTracer", "matte.extract": "a matte cut-out"}
    return fit.get("model") or fit.get("engine") or tools.get(rec["receipt"], rec["receipt"])


def asset_findings(ws: Workspace, canvas: Canvas) -> list[Finding]:
    """Provenance of every image and clip layer, from the registry row that made its asset."""
    out: list[Finding] = []
    reg = Registry(ws.assets)
    for layer in canvas.layers:
        if isinstance(layer, VideoLayer) and layer.matte == "person":
            out.append(Finding(f"rule.matte.{layer.id}", "fail", "MATTE-RISK",
                               f"{layer.id}: the speaker is cut out of the source by a video matting model; edges may show.",
                               {"layer": layer.id, "model": "RobustVideoMatting (mobilenetv3)"}, tier="rule"))
            continue
        is_vec = isinstance(layer, ShapeLayer) and layer.shape == "svg"
        if not isinstance(layer, (ImageLayer, ClipLayer)) and not is_vec:
            continue
        rec = reg.get(layer.asset)
        fit, lineage = json.loads(rec["fit"]), json.loads(rec["derived_from"])
        ev = {"layer": layer.id, "ref": layer.asset, "prov": rec["prov"], "receipt": rec["receipt"],
              "model": fit.get("model"), "prompt": fit.get("prompt"), "derived_from": lineage}
        op = fit.get("op")
        if rec["receipt"].startswith("asset_lab"):
            out.append(Finding(f"rule.needs_source.{layer.id}", "fail", "NEEDS-SOURCE:logo",
                               f"Brand mark rebuilt from the source ({rec['prov']}); upload the master file (SVG/AI/PDF) "
                               "to swap it in.", ev, bumps=["mark"], tier="rule"))
        elif rec["prov"] in ("generated", "generative_motion") and not _from_source(ws, lineage):
            out.append(Finding(f"rule.gen_created.{layer.id}", "fail", "GEN-CREATED",
                               f"{layer.id} is a new element made by {_maker(fit, rec)} ({op}); it is not in the source.",
                               ev, tier="rule"))
        elif rec["prov"] in ("generative_recovery", "generative_motion", "traced", "generated"):
            what = OP_FLAG.get(op or "", "split")          # a creation guided by source crops is a redraw of them
            measures = {k: fit[k] for k in ("fidelity", "seam", "matte", "exact_box") if k in fit}
            out.append(Finding(f"rule.gen_recovered.{layer.id}", "fail", f"GEN-RECOVERED:{what}",
                               f"{layer.id}: source content {dict(split='redrawn', fill='with regenerated areas', extend='extended', animate='animated', trace='re-drawn as vector curves')[what]} "
                               f"by {_maker(fit, rec)}.", {**ev, **measures}, tier="rule"))
            if what == "animate":
                out.append(Finding(f"rule.motion_approx.{layer.id}", "fail", "MOTION-APPROX",
                                   f"{layer.id}: motion of a source element was generated, not recorded.", ev, tier="rule"))
        matte = fit.get("matte") or {}
        alpha = fit.get("alpha") or {}
        if rec["prov"] == "segmented" or matte:
            risky = matte.get("holes_max", 0) > 0.01 or matte.get("key_noise_max", 0) > 3 or alpha.get("soft_share", 0) > 0.35
            if risky:
                out.append(Finding(f"rule.matte.{layer.id}", "fail", "MATTE-RISK", f"{layer.id}: matte edges or holes may show.",
                                   {**ev, "matte": matte, "alpha": alpha}, tier="rule"))
        text = fit.get("text") or {}
        if text.get("found") and not (op == "extract" and text.get("match")):
            out.append(Finding(f"rule.gen_text.{layer.id}", "fail", "GEN-TEXT",
                               f"{layer.id}: a generative model drew text {text['found'][:6]}; text must be typeset from the "
                               "copy deck.", {**ev, "text": text}, tier="rule"))
        if fit.get("faces") and rec["prov"] in ("generated", "generative_recovery", "generative_motion"):
            out.append(Finding(f"rule.synth_person.{layer.id}", "fail", "SYNTH-PERSON",
                               f"{layer.id}: {fit['faces']} face(s) in generated pixels.", ev, tier="rule"))
    return out


def verify(ws: Workspace, output: str, version: int | None = None, actor: str = "verifier") -> dict[str, Any]:
    t_start = time.time()
    store = CanvasStore(ws.canvas)
    canvas = store.get(output, version)
    rdir = render_dir(ws, output, canvas.version)
    receipt = read_json(rdir / "receipt.json")
    if not receipt:
        raise RuntimeError(f"{output} v{canvas.version} has no render; render it before verifying")
    mp4 = Path(receipt["file"])
    profile = load_profile(canvas.profile)
    index = IndexStore(ws.index / "index.sqlite")
    findings: list[Finding] = []
    findings += lint.lint(mp4, canvas.length, profile)
    findings += inspect.declared(canvas, index, profile, ws=ws)
    findings += inspect.boundaries(canvas, index)
    findings += inspect.text_readback(canvas, mp4, profile["legibility"]["min_cap_height_px"])
    attest_f = inspect.attest(lambda: Compositor(ws, canvas), mp4, canvas.length,
                              n=int(lever("render.attest_resample").get("frames", 24)))
    findings.append(attest_f)
    if lever_serves("qc.output_asr_readback"):
        findings += inspect.audio_readback(canvas, index, mp4)
    findings += standing_findings(ws, canvas, index)

    ledger = Ledger(ws.ledger / "ledger.jsonl")
    raised: list[dict[str, Any]] = []
    for f in findings:
        if f.status not in ("fail", "cannot_tell", "info") or not f.kind:
            continue
        flag = flags.make(f.kind, f.summary, output=output, version=canvas.version, subject=f"{f.check}:{f.level or ''}",
                          evidence={"check": f.check, **f.evidence}, bumps=f.bumps, level=f.level, raised_by=actor)
        ledger.append("flag.raised", actor, flag.to_dict(), output=output, version=canvas.version)
        raised.append(flag.to_dict())
    # a re-verification of the same version clears what it no longer finds (the ledger keeps the history)
    new_ids = {f["id"] for f in raised}
    for f in flags.current_flags(ledger, output=output, version=canvas.version):
        if (f.get("version") == canvas.version and f.get("raised_by") == actor and f["status"] == "open"
                and f["id"] not in new_ids):
            ledger.append("flag.cleared", actor, {"id": f["id"], "why": "not found by this verification"},
                          output=output, version=canvas.version)
    # request-level flags (index degradation, asks) apply to every output
    current = [f for f in flags.current_flags(ledger, output=output, version=canvas.version)
               if f.get("version") in (canvas.version, None)]
    attested = attest_f.status == "pass"
    colour = flags.triage(current, attested=attested, checks_qualified=False)
    result = {"output": output, "version": canvas.version, "file": str(mp4), "sha256": receipt["sha256"],
              "triage": colour, "attested": attested, "verifier": VERIFIER_VERSION,
              "findings": [f.to_dict() for f in findings], "flags": current,
              "seconds": round(time.time() - t_start, 1)}
    qdir = ws.qc / output / f"v{canvas.version}"
    write_json(qdir / "verification.json", result)
    cue = documents.cue_sheet(ws, canvas, index, receipt)
    write_json(qdir / "cue_sheet.json", cue)
    note = documents.delivery_note(ws, canvas, receipt, result, cue)
    (qdir / "delivery_note.md").write_text(note)
    side = sidecars.write(ws, canvas, mp4, qdir)
    manifest = {"output": output, "version": canvas.version, "triage": colour,
                "files": {"video": {"path": ws.rel(mp4), "sha256": receipt["sha256"]},
                          "delivery_note": {"path": ws.rel(qdir / "delivery_note.md"), "sha256": sha256_file(qdir / "delivery_note.md")},
                          "cue_sheet": {"path": ws.rel(qdir / "cue_sheet.json"), "sha256": sha256_file(qdir / "cue_sheet.json")},
                          "verification": {"path": ws.rel(qdir / "verification.json"), "sha256": sha256_file(qdir / "verification.json")},
                          **{k: {"path": ws.rel(v), "sha256": sha256_file(v)} for k, v in side.items()}},
                "flags": {lvl: sum(1 for f in current if f["severity"] == lvl and f["status"] == "open") for lvl in flags.LEVELS},
                "canvas_sha256": canvas.provenance.get("sha256"), "verifier": VERIFIER_VERSION}
    manifest["signature"] = sign(manifest)
    write_json(qdir / "manifest.json", manifest)
    ledger.append("verify.done", actor, {"triage": colour, "attested": attested, "flags": manifest["flags"],
                                         "manifest_sha256": sha256_file(qdir / "manifest.json")},
                  output=output, version=canvas.version, signed=True)
    return result
