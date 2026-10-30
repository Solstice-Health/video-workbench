"""Canvas, copy, render, editor-QC, flags and delivery tools (vwb.canvas/2).

The agent authors work/outputs/<id>/canvas.json itself (file tools or canvas_new), mints
strings with copy_add, checks with canvas_check, looks with canvas_preview, and commits
with canvas_commit. Only the job lead commits. Editor QC returns notices; deliver_submit
hands a version to the control plane, whose verifier alone derives flags and triage.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from vwb.canvas import copydeck
from vwb.canvas.compile import check, expand, load_working, plan, working_path
from vwb.canvas.model import font_library
from vwb.canvas.paths import output_id, read_record, version_number
from vwb.canvas.renderers import RENDERERS, get_renderer, validate_renderer
from vwb.canvas.renderers import catalog as renderer_catalog
from vwb.canvas.store import CanvasStore
from vwb.config import DOCS_DIR, load_profile
from vwb.index.paths import SourceIndexes, source
from vwb.media.decode import read_frames
from vwb.media.frames import contact_sheet, jpeg_bytes, resize_to_width
from vwb.qc import inspect, lint
from vwb.render import pipeline, plates
from vwb.render.compositor import Compositor
from vwb.tools.base import Tool, ToolContext, ToolResult, parse_json_arg, schema
from vwb.util.jsonio import write_json
from vwb.util.timecode import fmt
from vwb.verify.verifier import verify
from vwb.workspace import flags
from vwb.workspace.registry import Registry


def _store(ctx: ToolContext) -> CanvasStore:
    return CanvasStore(ctx.ws.canvas)


def _profile(ctx: ToolContext) -> dict[str, Any]:
    spec = ctx.ws.load().spec
    return load_profile((spec.profile if spec and spec.profile else None) or "youtube_shorts")


# ---------------------------------------------------------------- authoring

def canvas_schema(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    return ToolResult(text=(DOCS_DIR / "CANVAS.md").read_text())


def canvas_renderers(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from vwb.desktop.after_effects import AfterEffects
    bridge = getattr(ctx.state.get("integration_service"), "after_effects", None)
    host = (bridge if bridge is not None else AfterEffects()).status()
    return ToolResult.json(renderer_catalog(after_effects_status=host))


def canvas_set_renderer(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Change only the working document. Immutable versions and their receipts remain intact."""
    try:
        renderer = get_renderer(a["renderer"])
        c = load_working(ctx.ws, a["output"])
        c.renderer, c.renderer_pin = renderer.id, renderer.pin
        validate_renderer(c)
        data = read_record(ctx.ws.work, "outputs", output_id(a["output"]), "canvas.json")
        data.update(renderer=renderer.id, renderer_pin=renderer.pin)
        data.pop("renderer_environment", None)
        write_json(working_path(ctx.ws, a["output"]), data)
    except (ValueError, FileNotFoundError) as exc:
        return ToolResult.error(str(exc))
    return ToolResult.json({"output": a["output"], "renderer": renderer.id, "renderer_pin": renderer.pin,
                            "note": "Working canvas updated. Preview and commit a new version to render this choice; previous versions are unchanged."})


def layout_facts(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Facts for composing, not a layout: canvas size, safe zone, UI bands, floors, fonts, the source's look, assets."""
    prof = _profile(ctx)
    W, H = prof["width"], prof["height"]
    sz = prof["safe_zone"]
    src = source(ctx.ws, a.get("media_id"), require_explicit=True) if ctx.ws.load().sources else None
    st = SourceIndexes(ctx.ws).for_media(src.ref) if src else None
    styles = [json.loads(r["json"]).get("style") for r in st.all("SELECT json FROM readings WHERE scope='video'")] if st else []
    from vwb.workspace.registry import Registry
    assets = [{"ref": f"slot:{r['slot']}@{r['rev']}", "kind": r["kind"], "prov": r["prov"],
               "size": json.loads(r["fit"]).get("native_size")} for r in Registry(ctx.ws.assets).list()]
    m = st.media() if st else None
    return ToolResult.json({
        "canvas": [W, H], "fps": prof["fps"], "max_duration_s": prof["max_duration_s"],
        "renderers": renderer_catalog(),
        "safe_zone": {"x": sz["left"], "y": sz["top"], "w": W - sz["left"] - sz["right"], "h": H - sz["top"] - sz["bottom"],
                      "source": sz["source"]},
        "platform_ui": "Shorts overlays: header icons at the top; channel name, title and description at the bottom; "
                       "like/comment/share buttons down the right edge in the lower half. Keep text, marks and calls to "
                       "action inside the safe zone.",
        "legibility": prof["legibility"], "loudness": prof["loudness"],
        "fonts": {fam: {"category": f["category"], "weights": f["weights"], "italic": f["italic"]}
                  for fam, f in font_library().items()},
        "fonts_note": "Open-licence library; no family is a default. Match the source's typeface where you can.",
        "source": {"media": src.ref, "size": [m["width"], m["height"]], "fps": m["fps"]} if m else None,
        "timeline": "source_spans" if m else "authored_duration",
        "timeline_note": "Use source frame spans." if m else "Use canvas_new with duration_s. Imported designs are references; no source video or narration is required.",
        "brand_style_from_readings": [s for s in styles if s],
        "assets": assets,
    })


def canvas_new(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    try:
        renderer = get_renderer(a.get("renderer", "remotion"))
    except ValueError as exc:
        return ToolResult.error(str(exc))
    out = a["output"]
    p = working_path(ctx.ws, out)
    if p.exists() and not a.get("overwrite"):
        return ToolResult.error(f"{ctx.ws.rel(p)} exists; edit it, or pass overwrite=true")
    prof = _profile(ctx)
    req = ctx.ws.load()
    spans = parse_json_arg(a.get("spans") or [])
    explicit_map = parse_json_arg(a.get("time_map") or [])
    duration = a.get("duration_s")
    if duration is not None:
        if spans or explicit_map:
            return ToolResult.error("Choose duration_s for a new video or spans for a source edit, not both.")
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or not 0 < duration <= prof["max_duration_s"] or round(duration * prof["fps"]) < 1):
            return ToolResult.error(f"duration_s must cover at least one frame and be at most {prof['max_duration_s']} seconds.")
        n, src, time_map = round(duration * prof["fps"]), None, []
    elif explicit_map:
        if spans or a.get("media_id"):
            return ToolResult.error("Choose time_map with explicit media per span, or spans with media_id.")
        time_map = []
        for row in explicit_map:
            if (not isinstance(row, dict) or set(row) != {"media", "src_in", "src_out"}
                    or not isinstance(row["media"], str) or not row["media"]
                    or type(row["src_in"]) is not int or type(row["src_out"]) is not int
                    or not 0 <= row["src_in"] < row["src_out"]):
                return ToolResult.error("time_map needs {media, src_in, src_out} with explicit sources and 0 <= src_in < src_out.")
            time_map.append({**row, "media": source(ctx.ws, row["media"]).ref})
        n, src = sum(row["src_out"] - row["src_in"] for row in time_map), None
    else:
        if not req.sources or not spans:
            return ToolResult.error("For a new video without source cuts, pass duration_s. For a source edit, attach a video and pass spans.")
        if (not isinstance(spans, list) or any(not isinstance(pair, list) or len(pair) != 2
                or any(type(v) is not int for v in pair) or not 0 <= pair[0] < pair[1] for pair in spans)):
            return ToolResult.error("spans must contain [start, end] frame pairs with 0 <= start < end.")
        src = source(ctx.ws, a.get("media_id"), require_explicit=True)
        time_map = [{"media": src.ref, "src_in": x, "src_out": y} for x, y in spans]
        n = sum(y - x for x, y in spans)
    doc = {"schema": "vwb.canvas/2", "profile": prof["id"], "renderer": renderer.id, "renderer_pin": renderer.pin,
           "format": {"width": prof["width"], "height": prof["height"], "fps": prof["fps"]},
           "time_map": time_map, **({"duration_frames": n} if duration is not None else {}),
           "intent": a.get("intent", ""), "layers": []}
    write_json(p, doc)
    return ToolResult.json({"path": str(p.relative_to(ctx.ws.work)), "frames": n, "seconds": round(n / prof["fps"], 2),
                            "renderer": renderer.id, "renderer_pin": renderer.pin,
                            "media": src.ref if src else None, "sources": list(dict.fromkeys(s["media"] for s in time_map)),
                            "note": "Add layers by editing this file (see canvas_schema). Nothing is "
                                                      "placed for you: you design the composition."})


def copy_add(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    e = copydeck.mint(ctx.ws, copy_id=a["id"], role=a.get("role", "title"), source=a["source"], shot=a.get("shot"),
                      tracks=parse_json_arg(a.get("tracks")), sentences=parse_json_arg(a.get("sentences")), text=a.get("text"),
                      note=a.get("note"), media_id=a.get("media_id"))
    level = flags.text_unverified_severity(bool(e.agree), e.regulated)
    return ToolResult.json({**e.model_dump(), "unverified_level": level,
                            "note": "Cite it in a text layer as copy_id. Until a person confirms it, it carries "
                                    f"TEXT-UNVERIFIED at {level}."})


def copy_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    return ToolResult.json({"copy": [e.model_dump() for e in copydeck.load(ctx.ws).values()]})


def canvas_check(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Lint and measure the working canvas (nothing is committed or rendered)."""
    try:
        c = load_working(ctx.ws, a["output"])
    except (ValueError, FileNotFoundError) as exc:
        return ToolResult.error(str(exc))
    problems = [p.to_dict() for p in check(ctx.ws, c)]
    errors = [p for p in problems if p["severity"] == "error"]
    measures = []
    if not errors:
        c = expand(ctx.ws, c)
        st = SourceIndexes(ctx.ws)
        measures = [{"check": f.check, "status": f.status, "maps_to": f.kind, "summary": f.summary,
                     **({"detail": f.evidence} if f.status == "fail" else {})}
                    for f in inspect.declared(c, st, _profile(ctx), ws=ctx.ws) + inspect.boundaries(c, st)]
        caps = sum(len(l.pages or []) for l in c.layers if l.type == "captions")
        measures.append({"check": "copy", "status": "info",
                         "summary": f"{len(c.copy_deck)} typeset strings ({caps} caption pages)"})
    return ToolResult.json({"output": a["output"], "errors": errors, "warnings": [p for p in problems if p["severity"] == "warn"],
                            "measures": measures, "frames": c.length if not errors else None})


def _render_frames(ctx: ToolContext, c, frames: list[int], with_plates: bool = True) -> dict[int, np.ndarray]:
    """Composite chosen output frames of an expanded canvas; plates are rendered as stills of just those frames."""
    from vwb.design_context.checks import assert_renderable
    assert_renderable(ctx.ws, c)
    stills: dict[str, dict[int, np.ndarray]] = {}
    if with_plates:
        from vwb.render import environment
        if c.version:
            environment.validate(c)
        else:
            environment.bind(c)
        jobs, starts = [], {}
        for st in plan(c, Registry(ctx.ws.assets)):
            local = sorted({f - st.start for f in frames if st.kind == "plate" and st.start <= f < st.end})
            if local:
                jobs.append(("Scene", st.props, local))
                starts[st.plate_id] = st.start
        for pid, paths in plates.render_stills(ctx.ws.plates, jobs).items():
            stills[pid] = {starts[pid] + fr: np.asarray(Image.open(path).convert("RGBA")) for fr, path in paths.items()}
    return Compositor(ctx.ws, c, skip_plates=True).sample(frames, stills=stills)


def _working(ctx: ToolContext, output: str):
    c = load_working(ctx.ws, output)
    errors = [p for p in check(ctx.ws, c) if p.severity == "error"]
    if errors:
        raise ValueError("fix these first: " + "; ".join(f"{e.where}: {e.why}" for e in errors))
    return expand(ctx.ws, c)


def canvas_preview(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Render chosen frames of the working canvas (plates included) as a contact sheet."""
    try:
        c = _working(ctx, a["output"])
    except (ValueError, FileNotFoundError) as exc:
        return ToolResult.error(str(exc))
    if a.get("times"):
        frames = [min(c.length - 1, int(round(float(t) * c.fps))) for t in a["times"]]
    else:
        n = max(2, min(int(a.get("n", 6)), 12))
        frames = [int(c.length * (k + 0.5) / n) for k in range(n)]
    got = _render_frames(ctx, c, frames, with_plates=not a.get("no_text"))
    W, H = c.size
    prof = _profile(ctx)
    sz = prof["safe_zone"]
    cells = []
    for f in frames:
        img = Image.fromarray(got[f])
        if a.get("guides", True):
            d = ImageDraw.Draw(img)
            d.rectangle([sz["left"], sz["top"], W - sz["right"], H - sz["bottom"]], outline=(0, 255, 160), width=3)
        cells.append((f"{fmt(f / c.fps)}", np.asarray(img)))
    sheet = contact_sheet(cells, cols=min(int(a.get("cols", 3)), len(cells)), cell_width=int(a.get("cell_width", 320)))
    return ToolResult(text=f"{a['output']} working canvas via {c.renderer}, {len(cells)} frames (green = platform safe zone)",
                      images=[jpeg_bytes(sheet)])


def canvas_snapshot(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """One full-resolution frame of the working canvas as an image asset: a first or last frame for
    omni_video (animate a designed page, or move between two pages), or a reference for sunburst."""
    try:
        c = _working(ctx, a["output"])
    except (ValueError, FileNotFoundError) as exc:
        return ToolResult.error(str(exc))
    keep = set(a.get("layers") or [])
    drop_text = bool(a.get("no_text"))
    c.layers = [l for l in c.layers if (not keep or l.id in keep) and not (drop_text and l.type in ("text", "captions"))]
    if not c.layers:
        return ToolResult.error("no layers left to draw")
    f = min(c.length - 1, int(round(float(a.get("t", 0)) * c.fps))) if a.get("frame") is None else int(a["frame"])
    rgb = _render_frames(ctx, c, [f])[f]
    import tempfile

    from vwb.gen.refs import save_png
    from vwb.util.hashing import new_id
    with tempfile.TemporaryDirectory(dir=ctx.ws.assets) as tmp:
        path = save_png(np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)]), Path(tmp) / "snapshot.png")
        slot = (a.get("slot") or "").strip() or new_id(f"snap-{a['output']}", 2)
        ref = Registry(ctx.ws.assets).register(
            path, slot=slot, kind="image", receipt="canvas.snapshot",
            derived_from=[{"kind": "canvas", "output": a["output"], "frame": f, "layers": sorted(keep) or "all",
                           "no_text": drop_text}],
            fit={"native_size": [rgb.shape[1], rgb.shape[0]], "op": "snapshot", "frame": f})
    return ToolResult.json({"ref": ref, "frame": f, "size": [rgb.shape[1], rgb.shape[0]],
                            "use": "first/last frame for omni_video (task frames), or a reference for sunburst_create. "
                                   "Snapshot without text (no_text) when the model will move the picture, and keep "
                                   "text typeset on top: a video model distorts text."},
                           images=[jpeg_bytes(resize_to_width(rgb, 540))], artifacts=[ref])


def canvas_commit(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    outputs = a.get("outputs") or [a["output"]]
    rows, arts = [], []
    for out in outputs:
        try:
            c, warns = _store(ctx).commit(ctx.ws, out, author=ctx.agent, message=a.get("message", ""))
        except (ValueError, FileNotFoundError) as exc:
            rows.append({"output": out, "error": str(exc)})
            continue
        req = ctx.ws.load()
        if out not in req.outputs:
            req.outputs.append(out)
            ctx.ws.save(req)
        ctx.ledger.append("canvas.commit", ctx.agent, {"sha256": c.provenance.get("sha256"), "frames": c.length,
                                                        "message": a.get("message", "")}, output=out, version=c.version)
        rows.append({"output": out, "version": c.version, "seconds": round(c.length / c.fps, 2), "warnings": warns})
        arts.append(c.provenance.get("sha256", ""))
    return ToolResult.json({"commits": rows, "next": "render_output, qc_run / qc_peek, then deliver_submit"}, artifacts=arts)


def canvas_get(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    c = _store(ctx).get(a["output"], a.get("version"))
    d = c.dump()
    if not a.get("full"):
        for l in d["layers"]:
            if l.get("type") == "captions" and l.get("pages"):
                l["pages"] = f"{len(l['pages'])} pages"
    return ToolResult.json(d)


def canvas_query(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Find layers in a large canvas by time, type, role, id, asset, copy or region (a structural index)."""
    import fnmatch

    from vwb.canvas.anim import active, all_values, value_at
    try:
        c = (_store(ctx).get(a["output"], int(a["version"])) if a.get("version") else load_working(ctx.ws, a["output"]))
    except (ValueError, FileNotFoundError, KeyError) as exc:
        return ToolResult.error(str(exc))
    deck = copydeck.load(ctx.ws)
    f = int(round(float(a["at"]) * c.fps)) if a.get("at") is not None else None
    region = a.get("region")
    rows = []
    for z, l in enumerate(c.layers):
        if a.get("type") and l.type != a["type"]:
            continue
        if a.get("role") and l.role != a["role"]:
            continue
        if a.get("id") and not fnmatch.fnmatch(l.id, a["id"]):
            continue
        if a.get("asset") and getattr(l, "asset", None) != a["asset"]:
            continue
        copy_id = getattr(l, "copy_id", None)
        if a.get("copy_id") and copy_id != a["copy_id"]:
            continue
        text = deck[copy_id].text if copy_id in deck else None
        if a.get("text") and (not text or a["text"].lower() not in text.lower()):
            continue
        if f is not None and (not active(l.span, f) or float(value_at(l.opacity, f)) <= 0.01):
            continue
        boxes = [value_at(l.box, f)] if f is not None else all_values(getattr(l, "box", None))
        if region:
            rx, ry, rw, rh = region
            if not any(b and b[0] < rx + rw and rx < b[0] + b[2] and b[1] < ry + rh and ry < b[1] + b[3] for b in boxes):
                continue
        span = l.span or [0, c.length]
        rows.append({"z": z, "id": l.id, "type": l.type, "role": l.role,
                     "span_s": [round(span[0] / c.fps, 2), round(span[1] / c.fps, 2)],
                     "box": [round(v) for v in boxes[0]] if boxes and boxes[0] else None,
                     **({"asset": l.asset} if getattr(l, "asset", None) else {}),
                     **({"copy_id": copy_id, "text": text[:80]} if copy_id else {}),
                     **({"shape": l.shape} if l.type == "shape" else {})})
    limit = int(a.get("limit", 200))
    return ToolResult.json({"output": c.output, "source": f"v{a['version']}" if a.get("version") else "working",
                            "layers_total": len(c.layers), "matched": len(rows), "rows": rows[:limit]})


def shape_kit(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Correct geometry for common data graphics, as layers to paste into a canvas."""
    from vwb.canvas import shapekit
    kind = a["kind"]
    if kind not in shapekit.KINDS:
        return ToolResult.error(f"kind must be one of {sorted(shapekit.KINDS)}")
    params = {k: v for k, v in parse_json_arg(a.get("params") or {}).items()}
    try:
        layers = shapekit.KINDS[kind](a.get("prefix", kind), **params)
    except TypeError as exc:
        return ToolResult.error(f"{kind}: {exc}")
    return ToolResult.json({"kind": kind, "layers": layers,
                            "use": "Append these layers to the canvas (they are ordinary shape layers: edit ids, colours, "
                                   "boxes and keyframes freely), then canvas_preview."})


def canvas_diff(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    return ToolResult.json(_store(ctx).diff(a["output"], int(a["a"]), int(a["b"])))


def canvas_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    store = _store(ctx)
    working = sorted(p.parent.name for p in (ctx.ws.work / "outputs").glob("*/canvas.json")) if (ctx.ws.work / "outputs").exists() else []
    rows = []
    for out in sorted(set(working) | set(store.outputs())):
        v = store.head(out)
        ver = read_record(ctx.ws.root, "qc", output_id(out), f"v{version_number(v)}", "verification.json", default={}) if v else {}
        rows.append({"output": out, "working_file": out in working, "head": v,
                     "rendered": bool(v) and (pipeline.render_dir(ctx.ws, out, v) / "receipt.json").exists(),
                     "triage": ver.get("triage")})
    return ToolResult.json({"outputs": rows})


# ---------------------------------------------------------------- render, QC, delivery

def render_output(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    outputs = a.get("outputs") or [a["output"]]
    if a.get("version") and len(outputs) == 1:
        rs = [pipeline.render(ctx.ws, outputs[0], a.get("version"), actor=ctx.agent, force=bool(a.get("force")))]
    else:
        rs = pipeline.render_many(ctx.ws, outputs, actor=ctx.agent, force=bool(a.get("force")))
    rows = [{"output": r["output"], "version": r["version"], **({"error": r["error"]} if "error" in r else
            {"file": ctx.ws.rel(r["file"]), "seconds": r["seconds"], "render_seconds": r["render_seconds"]})} for r in rs]
    return ToolResult.json({"renders": rows}, artifacts=[r["sha256"] for r in rs if "sha256" in r])


def render_info(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    output = output_id(a["output"])
    v = _store(ctx).head(output) if a.get("version") is None else version_number(a["version"])
    if v is None:
        return ToolResult.json({"receipt": {}, "triage": None, "open_flags": []})
    rec = pipeline.render_receipt(ctx.ws, output, v, {})
    ver = read_record(ctx.ws.root, "qc", output, f"v{v}", "verification.json", default={})
    return ToolResult.json({"receipt": rec, "triage": ver.get("triage"),
                            "open_flags": [f"{f['severity']} {f['kind']}: {f['summary'][:100]}" for f in ver.get("flags", [])
                                           if f["status"] == "open"]})


def qc_run(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Editor QC on the shared engine. Notices only: it never raises or clears a flag."""
    if a.get("outputs"):
        runs = [qc_run(ctx, {**a, "outputs": None, "output": o}) for o in a["outputs"]]
        return ToolResult.json({"runs": [{"output": r.data["output"], "version": r.data["version"], "failing": r.data["failing"]}
                                         if not r.is_error else {"error": r.text} for r in runs]})
    c = _store(ctx).get(a["output"], a.get("version"))
    rec = pipeline.render_receipt(ctx.ws, c.output, c.version)
    if not rec:
        return ToolResult.error("render the output first (render_output)")
    mp4 = Path(rec["file"])
    prof = load_profile(c.profile)
    st = SourceIndexes(ctx.ws)
    found = lint.lint(mp4, c.length, prof) + inspect.declared(c, st, prof, ws=ctx.ws) + inspect.boundaries(c, st)
    if a.get("readback", True):
        found += inspect.text_readback(c, mp4, prof["legibility"]["min_cap_height_px"])
    if a.get("attest"):
        found.append(inspect.attest(lambda: Compositor(ctx.ws, c), mp4, c.length, n=8))
    if a.get("audio"):
        found += inspect.audio_readback(c, st, mp4)
    rows = [{"check": f.check, "status": f.status, "maps_to": f.kind, "summary": f.summary} for f in found]
    return ToolResult.json({"output": c.output, "version": c.version, "findings": rows,
                            "failing": [r for r in rows if r["status"] == "fail"],
                            "note": "Editor QC steers; the verifier decides at deliver_submit."})


def qc_peek(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    if a.get("outputs"):
        parts = [qc_peek(ctx, {"output": o, "n": a.get("n", 6)}) for o in a["outputs"]]
        return ToolResult(text="\n".join(p.text for p in parts), images=[i for p in parts for i in p.images])
    c = _store(ctx).get(a["output"], a.get("version"))
    rec = pipeline.render_receipt(ctx.ws, c.output, c.version)
    if not rec:
        return ToolResult.error("render the output first (render_output)")
    if a.get("times"):
        frames = [int(round(float(t) * c.fps)) for t in a["times"]]
    else:
        n = max(2, min(int(a.get("n", 8)), 16))
        frames = [int(c.length * (k + 0.5) / n) for k in range(n)]
    got = read_frames(rec["file"], frames, size=(540, 960))
    sheet = contact_sheet([(fmt(f / c.fps), got[f]) for f in frames if f in got], cols=min(4, len(frames)), cell_width=270)
    return ToolResult(text=f"{c.output} v{c.version}: {len(frames)} frames", images=[jpeg_bytes(sheet)])


def flags_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from vwb.workspace.ledger import Ledger
    fl = flags.current_flags(Ledger(ctx.ws.ledger / "ledger.jsonl"), output=a.get("output"))
    if a.get("status", "open") != "all":
        fl = [f for f in fl if f["status"] == a.get("status", "open")]
    fl.sort(key=lambda f: (-flags.LEVELS.index(f["severity"]), f["kind"]))
    return ToolResult.json({"flags": [{k: f.get(k) for k in ("id", "severity", "kind", "summary", "output", "version", "status")}
                                      for f in fl]})


def flags_note(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Agents may add a flag (never lower or clear one). Severity comes from the catalog."""
    f = flags.make(a["kind"], a["summary"], output=a.get("output"), subject=f"agent:{a['summary'][:60]}",
                   evidence=parse_json_arg(a.get("evidence") or {}), raised_by=ctx.agent)
    ctx.ledger.append("flag.raised", ctx.agent, f.to_dict(), output=a.get("output"))
    return ToolResult.json(f.to_dict())


def _deliver_one(ctx: ToolContext, output: str, version: int | None) -> ToolResult:
    c = _store(ctx).get(output, version)
    if not (pipeline.render_dir(ctx.ws, c.output, c.version) / "receipt.json").exists():
        pipeline.render(ctx.ws, c.output, c.version, actor=ctx.agent)
    r = verify(ctx.ws, c.output, c.version)
    open_flags = [f for f in r["flags"] if f["status"] == "open"]
    return ToolResult.json({"output": c.output, "version": c.version, "triage": r["triage"], "attested": r["attested"],
                            "open_flags": [f"{f['severity']} {f['kind']}: {f['summary'][:110]}" for f in
                                           sorted(open_flags, key=lambda f: -flags.LEVELS.index(f["severity"]))],
                            "delivery_note": ctx.ws.rel(ctx.ws.qc / c.output / f"v{c.version}" / "delivery_note.md")},
                           artifacts=[r["sha256"]])


def deliver_submit(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Hand rendered versions to the control plane: the verifier checks each and builds its review package."""
    outputs = a.get("outputs")
    from vwb.review import page
    if outputs:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            rs = list(pool.map(lambda o: _deliver_one(ctx, o, None), outputs))
        review = page.build(ctx.ws)
        return ToolResult.json({"submitted": [r.data if not r.is_error else {"error": r.text} for r in rs],
                                "review_page": str(review), "review_page_in_work": "(outside the work folder; the "
                                "harness prints this path to the person)"}, artifacts=[x for r in rs for x in r.artifacts])
    r = _deliver_one(ctx, a["output"], a.get("version"))
    if not r.is_error and isinstance(r.data, dict):
        r.data["review_page"] = str(page.build(ctx.ws))
        r.text = ToolResult.json(r.data).text
    return r


OUT = {"output": {"type": "string"}, "outputs": {"type": "array", "items": {"type": "string"}}, "version": {"type": "integer"}}

TOOLS = [
    Tool("canvas_renderers", "List trusted canvas renderer adapters and their capabilities. Select one per output; unsupported layers block a swap.",
         schema({}), canvas_renderers, "canvas.inspect", "canvas"),
    Tool("canvas_set_renderer", "Swap the working canvas renderer without changing layers or prior versions. Preview, then commit a new version. Rejects incompatible layers.",
         schema({"output": {"type": "string"}, "renderer": {"type": "string", "enum": list(RENDERERS)}}, ["output", "renderer"]),
         canvas_set_renderer, "canvas.configure", "canvas"),
    Tool("canvas_schema", "The canvas file reference (vwb.canvas/2): layer types, properties, keyframes, eases, text "
         "styles, masks, effects. Read it before writing a canvas.", schema({}), canvas_schema, "tool", "canvas"),
    Tool("layout_facts", "Facts for composing (not a layout): canvas size, safe zone, platform UI, legibility floors, "
         "fonts, brand colours from the readings, recovered assets.", schema({}), layout_facts, "tool", "canvas"),
    Tool("canvas_new", "Create work/outputs/<output>/canvas.json with no layers. Use duration_s to make a new video from a brief or design references, without source video. For source edits, use spans instead. You then write the layers.", schema({"output": {"type": "string"}, "intent": {"type": "string"},
         "duration_s": {"type": "number", "exclusiveMinimum": 0},
         "renderer": {"type": "string", "enum": list(RENDERERS)},
         "spans": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}, "overwrite": {"type": "boolean"}},
         ["output"]), canvas_new, "tool", "canvas"),
    Tool("copy_add", "Mint a typeset string into the copy deck. source: shot_title (shot id: that slide's title, two "
         "OCR readers), ocr (text track ids), transcript (sentence ids), or authored (text, with a note saying where it "
         "came from, for example a value measured with chart_read; flagged for a person to confirm). "
         "role: title|label|callout|endcard|reference. Returns the entry and its TEXT-UNVERIFIED level.",
         schema({"id": {"type": "string"}, "role": {"type": "string"}, "source": {"type": "string",
                 "enum": ["shot_title", "ocr", "transcript", "authored"]}, "shot": {"type": "integer"},
                 "tracks": {"type": "array", "items": {"type": "integer"}},
                 "sentences": {"type": "array", "items": {"type": "integer"}}, "text": {"type": "string"},
                 "note": {"type": "string"}}, ["id", "source"]), copy_add, "tool", "canvas"),
    Tool("copy_list", "Every minted string with provenance and reader agreement.", schema({}), copy_list, "tool", "canvas"),
    Tool("shape_kit", "Geometry for data graphics, returned as shape layers to paste into a canvas: kind iso_bars "
         "(params: box, values, max_value, colors, depth, gap, start, stagger, grow, span) isometric 3D bars growing "
         "one after another; ring (box, fraction, color, track, thickness, start, fill_frames) a progress ring that "
         "fills; pictogram (box, rows, cols, filled, color, empty, shape dot|square, start, fill_frames) a highlighted "
         "grid; arrow (start_pt, end_pt, color, width, head, start, draw_frames). Values and placement are yours.",
         schema({"kind": {"type": "string", "enum": ["iso_bars", "ring", "pictogram", "arrow"]},
                 "prefix": {"type": "string"}, "params": {"type": "object"}}, ["kind", "params"]), shape_kit, "tool", "canvas"),
    Tool("canvas_query", "Find layers in a canvas (the working file, or a committed version) without reading all of it: "
         "at (seconds, layers visible then), type, role, id (glob, e.g. 'ref-*'), asset, copy_id, text (in its copy), "
         "region [x, y, w, h] (box overlaps). Returns z order, span, box, asset or copy text for each match.",
         schema({"output": {"type": "string"}, "version": {"type": "integer"}, "at": {"type": "number"},
                 "type": {"type": "string"}, "role": {"type": "string"}, "id": {"type": "string"},
                 "asset": {"type": "string"}, "copy_id": {"type": "string"}, "text": {"type": "string"},
                 "region": {"type": "array", "items": {"type": "number"}}, "limit": {"type": "integer"}}, ["output"]),
         canvas_query, "tool", "canvas"),
    Tool("canvas_snapshot", "Register one full-resolution frame of the working canvas (t seconds) as an image asset. "
         "layers=[ids] draws only those layers; no_text drops text and captions. Use it as the first or last frame of "
         "omni_video (animate a designed page, or a transition between two pages: snapshot both, then task=frames) "
         "or as a reference image for sunburst_create.",
         schema({"output": {"type": "string"}, "t": {"type": "number"}, "frame": {"type": "integer"},
                 "layers": {"type": "array", "items": {"type": "string"}}, "no_text": {"type": "boolean"},
                 "slot": {"type": "string"}}, ["output"]), canvas_snapshot, "tool", "canvas"),
    Tool("canvas_check", "Lint and measure a working canvas without rendering: schema and reference errors, squash, "
         "safe zone, and what each video window keeps, cuts or drops of the source text and faces, type sizes, motion.",
         schema({"output": {"type": "string"}}, ["output"]), canvas_check, "tool", "canvas"),
    Tool("canvas_preview", "Look at a working canvas: renders chosen frames (times in seconds, or n evenly spaced) with "
         "text plates, as a contact sheet with the safe zone outlined. no_text=true skips text for speed.",
         schema({"output": {"type": "string"}, "times": {"type": "array", "items": {"type": "number"}}, "n": {"type": "integer"},
                 "no_text": {"type": "boolean"}, "guides": {"type": "boolean"}, "cols": {"type": "integer"},
                 "cell_width": {"type": "integer"}}, ["output"]), canvas_preview, "render", "canvas"),
    Tool("canvas_commit", "Commit working canvases as new immutable versions (checks first; errors block). Job lead only.",
         schema({"output": {"type": "string"}, "outputs": {"type": "array", "items": {"type": "string"}},
                 "message": {"type": "string"}}), canvas_commit, "canvas.commit", "canvas"),
    Tool("canvas_get", "Read a committed canvas version (head by default).",
         schema({"output": {"type": "string"}, "version": {"type": "integer"}, "full": {"type": "boolean"}}, ["output"]),
         canvas_get, "tool", "canvas"),
    Tool("canvas_diff", "Structural diff between two committed versions.",
         schema({"output": {"type": "string"}, "a": {"type": "integer"}, "b": {"type": "integer"}}, ["output", "a", "b"]),
         canvas_diff, "tool", "canvas"),
    Tool("canvas_list", "Outputs: working files, committed heads, render and triage status.", schema({}),
         canvas_list, "tool", "canvas"),
    Tool("render_output", "Render committed head versions: text plates (Remotion, one batch), compositor, x264, audio "
         "(two-pass -14 LUFS), mux, receipt. outputs=[...] renders several in parallel.",
         schema({**OUT, "force": {"type": "boolean"}}), render_output, "render", "render"),
    Tool("render_info", "Receipt and verification summary of a rendered version.",
         schema({"output": {"type": "string"}, "version": {"type": "integer"}}, ["output"]), render_info, "tool", "render"),
    Tool("qc_run", "Editor QC on the shared inspection engine for rendered versions: delivery and degenerate-edit gates, "
         "what windows keep/cut/drop, type size, faces, safe zone, cut boundaries, OCR read-back of every typeset string. "
         "attest=true re-renders samples; audio=true transcribes the output. Notices only.",
         schema({**OUT, "readback": {"type": "boolean"}, "attest": {"type": "boolean"}, "audio": {"type": "boolean"}}),
         qc_run, "tool", "qc"),
    Tool("qc_peek", "Look at rendered outputs: n frames (or given times in seconds) as a contact sheet.",
         schema({**OUT, "n": {"type": "integer"}, "times": {"type": "array", "items": {"type": "number"}}}),
         qc_peek, "tool", "qc"),
    Tool("flags_list", "Current flags (open by default; status=all for every state), optionally for one output.",
         schema({"output": {"type": "string"}, "status": {"type": "string"}}), flags_list, "tool", "flags"),
    Tool("flags_note", "Add a flag from the catalog with evidence. Agents add flags; they never lower or clear one.",
         schema({"kind": {"type": "string"}, "summary": {"type": "string"}, "output": {"type": "string"},
                 "evidence": {"type": "object"}}, ["kind", "summary"]), flags_note, "tool", "flags"),
    Tool("deliver_submit", "Submit rendered versions for review. The control plane's verifier runs every check, derives "
         "flags and the triage colour, and writes the cue sheet, delivery note and signed manifest.",
         schema(OUT), deliver_submit, "render", "deliver"),
]

for tool in TOOLS:
    if tool.name == "canvas_new":
        tool.schema["properties"]["time_map"] = {"type": "array", "items": schema({
            "media": {"type": "string"}, "src_in": {"type": "integer"}, "src_out": {"type": "integer"}},
            ["media", "src_in", "src_out"])}
        tool.description += " For multi-source edits, pass time_map=[{media, src_in, src_out}, ...] instead of spans."
    if tool.name in {"layout_facts", "canvas_new", "copy_add"}:
        tool.schema["properties"]["media_id"] = {"type": "string", "description": "Source ID or immutable ref; required for source-backed operations when multiple videos are registered."}
        tool.description += " Use media_id to select a source when multiple videos are registered."
