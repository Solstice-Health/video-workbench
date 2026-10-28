"""Clip, reframe and asset-lab tools."""

from __future__ import annotations

from typing import Any

from vwb.assets.lab import recover_mark
from vwb.clip.boundaries import Refiner
from vwb.clip.candidates import generate, select_set
from vwb.clip.validate import validate_plan
from vwb.config import load_profile
from vwb.index.paths import SourceIndexes, source
from vwb.index.store import IndexStore
from vwb.reframe import solver
from vwb.reframe.focus import FocusFinder
from vwb.tools.base import Tool, ToolContext, ToolResult, parse_json_arg, schema
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import write_json
from vwb.util.timecode import fmt
from vwb.workspace.registry import Registry


def _store(ctx: ToolContext, a: dict[str, Any]) -> IndexStore:
    src = source(ctx.ws, a.get("media_id"), require_explicit=True)
    return SourceIndexes(ctx.ws).for_media(src.ref)


def _profile(ctx: ToolContext) -> dict[str, Any]:
    spec = ctx.ws.load().spec
    return load_profile((spec.profile if spec and spec.profile else None) or "youtube_shorts")


def clip_candidates(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    st = _store(ctx, a)
    dmin, dmax = float(a.get("dmin", 15)), float(a.get("dmax", 60))
    target = float(a.get("target", (dmin + dmax) / 2))
    cands = generate(st, dmin=dmin, dmax=dmax, target=target)
    n = int(a.get("n", 0) or 0)
    suggested = []
    if n:
        ref = Refiner(st, st.path.parent)
        for c in select_set(cands, n):
            cut = ref.refine(c.t0, c.t1)
            suggested.append({**c.to_dict(), "cut_frames": [cut.f_in, cut.f_out], "cut_notes": cut.notes,
                              "cut_risks": cut.risks})
    return ToolResult.json({"n_candidates": len(cands), "top": [c.to_dict() for c in cands[: int(a.get("limit", 25))]],
                            "suggested_set": suggested,
                            "note": "Deterministic scores nominate; you choose. Sentences are the units, so cuts start and "
                                    "end on sentence boundaries. Refine every span with clip_refine before use."})


def clip_refine(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    st = _store(ctx, a)
    cut = Refiner(st, st.path.parent).refine(float(a["t0"]), float(a["t1"]))
    fps = st.media()["fps"]
    return ToolResult.json({**cut.to_dict(), "t_in": fmt(cut.f_in / fps), "t_out": fmt(cut.f_out / fps),
                            "seconds": round((cut.f_out - cut.f_in) / fps, 3)})


def clip_validate(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    outputs = parse_json_arg(a["outputs"])
    prof = _profile(ctx)
    groups = {}
    for output in outputs:
        src = source(ctx.ws, output.get("media_id", a.get("media_id")), require_explicit=True)
        if any(not isinstance(pair, list) or len(pair) != 2 or any(type(v) is not int for v in pair)
               for pair in output.get("spans", [])):
            raise ValueError("clip_validate accepts frame pairs for one media_id per output; validate mixed-source timelines with canvas_check.")
        groups.setdefault(src.ref, []).append(output)
    rep = {"outputs": [], "pairs": [], "ok": True}
    for ref, group in groups.items():
        result = validate_plan(_store(ctx, {"media_id": ref}), group, float(a.get("dmin", 10)),
                               float(a.get("dmax", 60)), prof["max_duration_s"])
        rep["outputs"].extend({**row, "media": ref} for row in result["outputs"])
        rep["pairs"].extend({**row, "media": ref} for row in result["pairs"])
        rep["ok"] &= result["ok"]
    return ToolResult.json(rep)


def _solutions_dir(ctx: ToolContext):
    d = ctx.ws.work / "solutions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reframe_solve(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Advisor: a crop track for ONE video window at the box you chose (any position, size, aspect)."""
    jobs = parse_json_arg(a.get("jobs"))
    if jobs:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            rs = list(pool.map(lambda j: reframe_solve(ctx, {**a, **j, "jobs": None}), jobs))
        return ToolResult.json({"solutions": [{"key": j.get("key"), **(r.data if not r.is_error else {"error": r.text})}
                                              for j, r in zip(jobs, rs, strict=True)]}, artifacts=[x for r in rs for x in r.artifacts])
    st = _store(ctx, a)
    prof = _profile(ctx)
    spans = [tuple(int(v) for v in s) for s in parse_json_arg(a["spans"])]
    box = tuple(float(v) for v in parse_json_arg(a["box"]))
    sz = prof["safe_zone"]
    target = solver.Target(box=box, canvas_h=prof["height"],
                           safe=(sz["left"], sz["top"], prof["width"] - sz["left"] - sz["right"],
                                 prof["height"] - sz["top"] - sz["bottom"]))
    sol = solver.solve(st, spans, target, legibility_floor=prof["legibility"]["min_cap_height_px"],
                       k=a.get("k"), focus=a.get("focus", "auto"))
    d = sol.to_dict()
    d["media"] = source(ctx.ws, a.get("media_id"), require_explicit=True).ref
    d["spans"] = [list(s) for s in spans]
    sid = "sol-" + sha256_json(d)[:12]
    write_json(_solutions_dir(ctx) / f"{sid}.json", d)
    beats = [{"out": f"{fmt(b['o0'] / prof['fps'])}-{fmt(b['o1'] / prof['fps'])}", "shot": b["shot"],
              "focus": b["focus_kind"], "focus_box": b["focus_box"], "why": b["focus_reason"][:110]} for b in sol.beats]
    return ToolResult.json({"media": d["media"], "solution_id": sid, "saved": f"solutions/{sid}.json", "box": list(box),
                            "crop": sol.keyframes, "params": sol.params, "cost": sol.cost, "metrics": sol.metrics,
                            "risks": sol.risks[:20], "beats": beats,
                            "alternatives": [{"cost": alt["cost"], "params": alt["params"]} for alt in sol.alternatives],
                            "note": "Use `crop` as the video layer's crop keyframes with this box (fit=window). Adjust "
                                    "freely; canvas_check measures whatever you write."},
                           artifacts=[sid])


def focus_find(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Advisor: for each spoken beat in the spans, what on screen it refers to (source pixels)."""
    st = _store(ctx, a)
    spans = [tuple(int(v) for v in s) for s in parse_json_arg(a["spans"])]
    tm = solver.TimeMap(spans)
    beats = solver.build_beats(st, tm, FocusFinder(st, exclude_title=not a.get("include_title", False)))
    fps = st.media()["fps"]
    return ToolResult.json({"beats": [{"out": f"{fmt(b['o0'] / fps)}-{fmt(b['o1'] / fps)}", "out_frames": [b["o0"], b["o1"]],
                                       "shot": b["shot"], "text": b["text"][:160], "focus": b["focus_kind"],
                                       "focus_box": [round(v) for v in b["focus_box"]], "why": b["focus_reason"][:120],
                                       "tracks": b["focus_tracks"]} for b in beats]})


def asset_marks(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rows = _store(ctx, a).all("SELECT * FROM marks ORDER BY persistence DESC")
    return ToolResult.json({"marks": rows, "registry": Registry(ctx.ws.assets).list()})


def asset_recover(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    res = recover_mark(ctx.ws, int(a["mark_id"]), slot=a.get("slot"), media_id=a.get("media_id"))
    ctx.ledger.append("asset.recovered", ctx.agent, res)
    return ToolResult.json({**res, "note": "Two variants: `keyed` (the mark cut out of its background) and `badge` (the "
                                           "mark on its own background). Place either with an image layer, or rebuild "
                                           "a cleaner one with sunburst_extract. Every mark not uploaded by a person "
                                           "raises NEEDS-SOURCE:logo (HOLD)."},
                           artifacts=[res["badge"]])


TOOLS = [
    Tool("clip_candidates", "Score every sentence-aligned span in [dmin, dmax] seconds and explain each score. With n, "
         "also return the best set of n non-overlapping spans with refined cut frames.",
         schema({"dmin": {"type": "number"}, "dmax": {"type": "number"}, "target": {"type": "number"},
                 "n": {"type": "integer"}, "limit": {"type": "integer"}}), clip_candidates, "tool", "clip"),
    Tool("clip_refine", "Turn a span in seconds (sentence start to sentence end) into exact cut frames: in a speech gap, "
         "at an energy minimum, on a shot cut when one is near, with flash-frame guards. Returns risks.",
         schema({"t0": {"type": "number"}, "t1": {"type": "number"}}, ["t0", "t1"]), clip_refine, "tool", "clip"),
    Tool("clip_validate", "Validate a plan before any render: outputs=[{id, spans:[[f_in,f_out],...]}] in source frames. "
         "Checks cut-in-word, partial sentences, self-contained openings, duration, overlap between outputs.",
         schema({"outputs": {"type": "array", "items": {"type": "object"}}, "dmin": {"type": "number"},
                 "dmax": {"type": "number"}}, ["outputs"]), clip_validate, "tool", "clip"),
    Tool("reframe_solve", "Advisor: solve a crop track for one video window placed at box=[x,y,w,h] (output px) over "
         "spans=[[f_in,f_out],...]. focus: auto (follows the narration to what is on screen), presenter, or content. The "
         "camera holds during a spoken beat, moves between beats, cuts on source cuts and avoids cutting text lines or "
         "faces. Returns crop keyframes plus beats, metrics and risks. jobs=[{key, spans, box, focus}] solves many.",
         schema({"spans": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
                 "box": {"type": "array", "items": {"type": "number"}}, "focus": {"type": "string",
                 "enum": ["auto", "presenter", "content"]}, "k": {"type": "integer"},
                 "jobs": {"type": "array", "items": {"type": "object"}}}), reframe_solve, "render", "reframe"),
    Tool("focus_find", "Advisor: split the narration in the spans into beats and say what on screen each beat refers to "
         "(focus box in source pixels, matched text tracks, or the presenter).",
         schema({"spans": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
                 "include_title": {"type": "boolean"}}, ["spans"]), focus_find, "tool", "reframe"),
    Tool("asset_marks", "List mined marks (logos, page furniture) and every asset in the registry (with provenance).",
         schema({}),
         asset_marks, "tool", "assets"),
    Tool("asset_recover", "Recover a mark from the source (asset lab rungs 0-1: its largest clean instance, fused over "
         "neighbouring frames) into the registry as an immutable revision. Returns slot refs.",
         schema({"mark_id": {"type": "integer"}, "slot": {"type": "string"}}, ["mark_id"]),
         asset_recover, "asset.recover.pixels", "assets"),
]

for tool in TOOLS:
    tool.schema["properties"]["media_id"] = {"type": "string", "description": "Source ID or immutable ref; required when multiple sources are registered. Each reframe job or clip_validate output can also supply its own media_id."}
    tool.description += " Specify media_id when more than one source is registered."
