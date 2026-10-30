"""Frames, contact sheets and typed video questions (watch).

Claude and GPT cannot watch video, so they look at frames and contact sheets; the watch
tool sends a short window to a video-native model at the frame rate the question needs.
A watch answer is a claim: it nominates, a script or a person decides (spec D22).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from vwb.canvas.paths import checked_path
from vwb.gateway import gemini as gem
from vwb.media.decode import read_frames
from vwb.media.frames import contact_sheet, crop, draw_boxes, jpeg_bytes, png_bytes, resize_to_width
from vwb.media.inputs import ResolvedVideo, resolve_video
from vwb.media.probe import video_stream_info
from vwb.tools.base import Tool, ToolContext, ToolResult, schema
from vwb.tools.index_tools import read_source_index
from vwb.util.hashing import new_id, sha256_bytes, sha256_file
from vwb.util.jsonio import dumps, write_json
from vwb.util.timecode import fmt
from vwb.workspace.media import register_work_file
from vwb.workspace.request import now_iso


def media_register(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    src = register_work_file(ctx.ws, a["path"])
    return ToolResult.json({"media_id": src.media_id, "ref": src.ref, "sha256": src.sha256,
                            "path": a["path"], "notice": "Immutable source registered. Use index_build(media_id) to "
                            "build optional search/OCR indexes. Inspection accepts this ref, attachment:<id>, or a work path directly."},
                           artifacts=[src.sha256])


def _resolve_target(ctx: ToolContext, target: str | None) -> ResolvedVideo:
    """Pin a source/asset/file or an explicit rendered output version."""
    target = target or "source"
    if target.startswith("output:"):
        from vwb.canvas.store import CanvasStore
        from vwb.render.pipeline import render_dir
        body = target.removeprefix("output:")
        out, _, ver = body.partition("@")
        store = CanvasStore(ctx.ws.canvas)
        v = int(ver.lstrip("v")) if ver else store.head(out)
        if v is None:
            raise ValueError(f"no canvas for {out}")
        p = render_dir(ctx.ws, out, v) / f"{out}_v{v}.mp4"
        if not p.is_file():
            raise ValueError(f"{out} v{v} is not rendered yet")
        store.get(out, v)
        ref = f"output:{out}@v{v}"
        return ResolvedVideo(p, ref, {"kind": "output", "ref": ref, "output": out, "version": v})
    return resolve_video(ctx.ws, target)


def resolve_target(ctx: ToolContext, target: str | None) -> tuple[Path, float, str]:
    """Resolve inspectable video without creating or depending on an edit index."""
    resolved = _resolve_target(ctx, target)
    fps = float(video_stream_info(resolved.path).fps)
    label = resolved.ref.removeprefix("source:").removeprefix("output:")
    return resolved.path, fps, label


def media_frame(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    path, fps, label = resolve_target(ctx, a.get("target"))
    f = int(a["frame"]) if a.get("frame") is not None else int(round(float(a.get("t", 0)) * fps))
    rgb = read_frames(path, [f])[f]
    if a.get("show") and not a.get("target", "source").startswith("output:"):
        with read_source_index(ctx.ws, label) as st:
            boxes = []
            if "text" in a["show"]:
                boxes += [((t["x"], t["y"], t["w"], t["h"]), f"tt{t['id']}:{t['role']}") for t in
                          st.all("SELECT * FROM text_tracks WHERE f0 <= ? AND f1 > ?", (f, f))]
            if "blocks" in a["show"]:
                shot = st.one("SELECT id FROM shots WHERE f0 <= ? AND f1 > ?", (f, f))
                if shot:
                    boxes += [((b["x"], b["y"], b["w"], b["h"]), f"b{b['id']}:{b['kind']}") for b in
                              st.all("SELECT * FROM blocks WHERE shot=?", (shot["id"],))]
            if "faces" in a["show"]:
                boxes += [((x["x"], x["y"], x["w"], x["h"]), "face") for x in st.all("SELECT * FROM faces WHERE f BETWEEN ? AND ?", (f - 3, f + 3))]
            rgb = draw_boxes(rgb, boxes)
    if a.get("crop"):
        rgb = crop(rgb, a["crop"])
    width = int(a.get("width", 960))
    if rgb.shape[1] > width:
        rgb = resize_to_width(rgb, width)
    img = png_bytes(rgb) if a.get("png") else jpeg_bytes(rgb)
    target = label if label.startswith("slot:") else ("output:" if str(a.get("target", "")).startswith("output:") else "source:") + label
    return ToolResult(text=f"{label} frame {f} ({fmt(f / fps)}), shown {rgb.shape[1]}x{rgb.shape[0]}", images=[img],
                      data={"frame": f, "target": target})


def media_contact_sheet(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    path, fps, label = resolve_target(ctx, a.get("target"))
    t0, t1 = float(a.get("t0", 0)), float(a["t1"])
    n = max(2, min(int(a.get("n", 8)), 24))
    frames = [int(round((t0 + (t1 - t0) * (k + 0.5) / n) * fps)) for k in range(n)]
    got = read_frames(path, frames, size=None)
    cells = [(fmt(f / fps), got[f]) for f in frames if f in got]
    cols = int(a.get("cols", 4))
    sheet = contact_sheet(cells, cols=cols, cell_width=int(a.get("cell_width", 320)))
    target = label if label.startswith("slot:") else ("output:" if str(a.get("target", "")).startswith("output:") else "source:") + label
    return ToolResult(text=f"{label} {fmt(t0)}-{fmt(t1)}, {len(cells)} frames", images=[jpeg_bytes(sheet)],
                      data={"frames": frames, "target": target})


def output_compare(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Two rendered versions of an output side by side, and where they differ over time."""
    out = a["output"]
    pa, fps, la = resolve_target(ctx, f"output:{out}@v{int(a['a'])}")
    pb, _, lb = resolve_target(ctx, f"output:{out}@v{int(a['b'])}")
    from vwb.media.probe import probe
    n_a, n_b = probe(pa, with_keyframes=False).n_frames, probe(pb, with_keyframes=False).n_frames
    n = min(n_a, n_b)
    step = max(1, int(fps / 2))
    idx = list(range(0, n, step))
    fa, fb = read_frames(pa, idx, size=(270, 480)), read_frames(pb, idx, size=(270, 480))
    diffs = [(i, float(np.abs(fa[i].astype(np.int16) - fb[i].astype(np.int16)).mean())) for i in idx if i in fa and i in fb]
    changed, cur = [], None
    for i, d in diffs:
        if d > 2.0:
            cur = [i, i + step] if cur is None else [cur[0], i + step]
        elif cur is not None:
            changed.append(cur)
            cur = None
    if cur is not None:
        changed.append(cur)
    k = max(2, min(int(a.get("n", 6)), 12))
    picks = sorted(sorted(diffs, key=lambda x: -x[1])[:k], key=lambda x: x[0]) if changed else diffs[:: max(1, len(diffs) // k)][:k]
    full_a, full_b = read_frames(pa, [i for i, _ in picks]), read_frames(pb, [i for i, _ in picks])
    cells = []
    for i, d in picks:
        cells += [(f"{la} {fmt(i / fps)}", full_a[i]), (f"{lb} {fmt(i / fps)} Δ{d:.1f}", full_b[i])]
    sheet = contact_sheet(cells, cols=min(8, len(cells)), cell_width=int(a.get("cell_width", 240)))
    return ToolResult.json({"a": la, "b": lb, "frames": [n_a, n_b], "changed_ranges_s": [[round(x / fps, 2), round(y / fps, 2)] for x, y in changed],
                            "most_changed": [{"t": round(i / fps, 2), "mean_abs_diff": round(d, 2)} for i, d in picks]},
                           images=[jpeg_bytes(sheet)])


def chart_read(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from vwb.gen.refs import resolve
    from vwb.index.charts import annotate, read_chart
    src = resolve(ctx.ws, a["source"])
    res = read_chart(src.rgba[..., :3], a["box"], a.get("orientation", "auto"))
    note = ("Values are measurements from the chart's pixels (precision about the axis 'precision'), not printed "
            "figures: typeset them as authored copy with a note naming this chart and frame." if res["axis"] else
            "No numeric axis was read, so bars have extents but no values; widen the box to include the axis labels.")
    out = {**res, "source": a["source"], "note": note}
    for b in out["bars"]:
        b.pop("colour_id", None)
    for e in out["legend"]:
        e.pop("colour_id", None)
    img = annotate(src.rgba, res)
    if img.shape[1] > 1200:
        img = resize_to_width(img, 1200)
    return ToolResult.json(out, images=[jpeg_bytes(img)])


def _watch_number(value: Any, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'non-negative'} number.")
    return float(value)


def watch_ask(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    question = a["question"].strip()
    if not question:
        raise ValueError("Give a non-empty question about this video.")
    resolved = _resolve_target(ctx, a.get("target"))
    info = video_stream_info(resolved.path)
    fps = float(info.fps)
    t0 = _watch_number(a["t0"], "t0") if a.get("t0") is not None else 0.0
    t1 = _watch_number(a["t1"], "t1") if a.get("t1") is not None else float(info.duration)
    if not 0 <= t0 < t1 <= float(info.duration):
        raise ValueError(f"Choose 0 <= t0 < t1 <= {info.duration:g} seconds for this video.")
    sample_fps = _watch_number(a.get("fps", 2), "fps", positive=True)
    resolution = a.get("resolution", "high")
    if resolution not in {"low", "medium", "high"}:
        raise ValueError("resolution must be low, medium or high.")
    identity = {**resolved.lineage, "target": resolved.ref}
    if identity["kind"] == "output":
        identity["sha256"] = sha256_file(resolved.path)
    observation = new_id("watch", 12)
    folder = checked_path(ctx.ws.root, "memory", "media_observations")
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / f"{observation}.json"
    row = {"id": observation, "kind": "watch.answer", "request": ctx.ws.id,
           "created": now_iso(), "actor": ctx.agent,
           "identity": identity, "target": resolved.ref, "status": "pending", "question": question,
           "f0": int(t0 * fps), "f1": int(t1 * fps), "t0": t0, "t1": t1, "source_fps": fps,
           "settings": {"fps": sample_fps, "resolution": resolution}}
    # Persist/validate the destination before spending a model call. Observations
    # are request-local and target-specific, independent of any source index.
    write_json(receipt, row)
    try:
        uploaded = gem.upload(ctx.gateway, resolved.path)
        part = gem.video_part(uploaded, start=t0, end=t1, fps=sample_fps)
        prompt = (question + "\n\nAnswer precisely. Give times as MM:SS.s from the start of the full video. "
                  "If you cannot tell, say so. For locations give box_2d [ymin, xmin, ymax, xmax] normalised to 0-1000.")
        call = gem.generate(ctx.gateway, "watch", [part, prompt], purpose=f"watch:{ctx.agent}", resolution=resolution)
    except Exception:
        write_json(receipt, {**row, "status": "failed"})
        raise
    row.update(status="claim", answer=call.text, model=call.model)
    write_json(receipt, row)
    public_path = f"inspection/watch/{observation}.json"
    public_bytes = (dumps(row) + "\n").encode("utf-8")
    warnings = []
    try:
        from vwb.studio.assets import publish
        publish(ctx.ws, "work/" + public_path, public_bytes)
    except (OSError, ValueError):
        # A changed/unsafe work directory must not discard a successful paid
        # answer. The canonical private receipt and returned answer survive.
        public_path = None
        warnings.append("The answer was saved privately, but the work-folder receipt could not be published. "
                        "The answer below is complete; do not regenerate it just to recover the receipt.")
    return ToolResult.json({"answer": call.text, "model": call.model, "observation": observation,
                            "receipt": ctx.ws.rel(receipt), "target": resolved.ref, "identity": identity, "status": "claim",
                            "path": public_path, "receipt_sha256": sha256_bytes(public_bytes), "warnings": warnings,
                            "receipt_access": "Read path with file_read; discover completed receipts with file_list(glob='inspection/watch/*.json').",
                            "note": "A watch answer nominates. Confirm with frames, the index or a script before relying on it."},
                           artifacts=[sha256_bytes(call.text.encode())])


TOOLS = [
    Tool("media_register", "Optionally pin a downloaded or authored video under work/ as an immutable source, deduplicated "
         "by SHA-256. Pass a relative path (e.g. downloads/clip.mp4). Inspection and generation already accept paths "
         "directly and snapshot automatically. Use index_build(media_id) only for analysis that needs an index; "
         "registration itself makes no model calls.",
         schema({"path": {"type": "string"}}, ["path"]), media_register, "media.register", "media"),
    Tool("media_frame", "One frame as an image, from the source or a rendered output ('output:short-01' or "
         "'output:short-01@v2'). Give t (seconds) or frame. Optional crop [x,y,w,h], width, and show=['text','blocks',"
         "'faces'] to draw existing index boxes on a source frame. Plain inspection needs no index or manual registration.",
         schema({"target": {"type": "string"}, "t": {"type": "number"}, "frame": {"type": "integer"},
                 "crop": {"type": "array", "items": {"type": "number"}}, "width": {"type": "integer"},
                 "show": {"type": "array", "items": {"type": "string", "enum": ["text", "blocks", "faces"]}},
                 "png": {"type": "boolean"}}), media_frame, "tool", "media"),
    Tool("media_contact_sheet", "A grid of n frames (2-24) evenly spaced between t0 and t1 seconds, from the source or "
         "a rendered output.", schema({"target": {"type": "string"}, "t0": {"type": "number"}, "t1": {"type": "number"},
                                       "n": {"type": "integer"}, "cols": {"type": "integer"},
                                       "cell_width": {"type": "integer"}}, ["t1"]), media_contact_sheet, "tool", "media"),
    Tool("output_compare", "Compare two rendered versions of an output (a and b): the time ranges where they differ, "
         "and the most-changed frames side by side. For checking that a repair changed what it should and nothing else.",
         schema({"output": {"type": "string"}, "a": {"type": "integer"}, "b": {"type": "integer"}, "n": {"type": "integer"},
                 "cell_width": {"type": "integer"}}, ["output", "a", "b"]), output_compare, "tool", "media"),
    Tool("chart_read", "Measure a bar chart in a frame or asset: bars (box, colour, series from the legend, category "
         "label), the value axis fitted from its tick labels, and each bar's value read through that fit, with an "
         "annotated preview. For recreating an unlabelled chart in another form (flat, 3D, animated) without guessing "
         "values. box [x, y, w, h] should include the axis labels and legend.",
         schema({"source": {"type": "string", "description": "A work-relative image path (work: or work/ optional), attachment:<id>, or slot:<name>@<rev>; for video append #t=<seconds> or #f=<frame> to a source:<media ref>, attachment, work path or video slot. t:/f: selects the sole registered source."},
                 "box": {"type": "array", "items": {"type": "number"}},
                 "orientation": {"type": "string", "enum": ["auto", "vertical", "horizontal"]}}, ["source", "box"]),
         chart_read, "tool", "media"),
    Tool("watch_ask", "Ask a video-native model (Gemini) a question about a window of the source or a rendered output, "
         "at the frame rate the question needs (default 2 fps, high resolution). The answer is stored as a claim "
         "in a request-local receipt pinned to the exact target, independently of the edit index. "
         "Completed receipts are also readable with file_read at the returned path under inspection/watch/.",
         schema({"question": {"type": "string"}, "target": {"type": "string"}, "t0": {"type": "number"},
                 "t1": {"type": "number"}, "fps": {"type": "number"},
                 "resolution": {"type": "string", "enum": ["low", "medium", "high"]}}, ["question"]),
         watch_ask, "watch", "media"),
]

for tool in TOOLS:
    if "target" in tool.schema["properties"]:
        tool.schema["properties"]["target"]["description"] = "source (first source), source:<media_id or ref>, slot:<name>@<rev>, attachment:<id>, a work-relative video path (work: or work/ optional), or output:<id>[@v<N>]."
