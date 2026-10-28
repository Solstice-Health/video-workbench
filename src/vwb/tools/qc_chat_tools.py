"""Shared inspection tools and an explicit, filtered findings handoff."""
from __future__ import annotations

from typing import Any

from vwb.media.decode import read_frames
from vwb.media.frames import contact_sheet, crop, jpeg_bytes
from vwb.qc.chat import family, model_identity, rendered_path
from vwb.qc.chat_store import QCStore
from vwb.tools.base import Tool, ToolContext, ToolResult, schema
from vwb.util.jsonio import append_jsonl
from vwb.workspace.request import now_iso


def _parent(ctx: ToolContext):
    return ctx.state.get("qc_parent", ctx.ws)


def _targets(ctx: ToolContext) -> list[dict]:
    return ctx.state["qc_targets"] if "qc_targets" in ctx.state else QCStore(ctx.ws).targets()


def qc_targets(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult.json({"targets": _targets(ctx), "note": "Choose an explicit version. Inspection is sampled; no pass is implied."})


def _target(ctx: ToolContext, target: str) -> tuple[dict, Any]:
    row = next((r for r in _targets(ctx) if r["target"] == target), None)
    if row is None:
        raise ValueError("The target is not in this turn's frozen media snapshot.")
    author_family = family(row.get("author_model") or "")
    reviewer_family = family(ctx.state.get("qc_model", ""))
    same_model = (bool(row.get("author_model"))
                  and model_identity(row["author_model"]) == model_identity(ctx.state.get("qc_model", "")))
    if ctx.role == "qc_lead" and (same_model or author_family and reviewer_family == author_family):
        raise ValueError("This version was authored by the QC model's family. Configure a different QC model family.")
    try:
        current = QCStore(_parent(ctx)).verify_target(target)
    except ValueError as exc:
        raise ValueError("The pinned media changed or is unavailable. Start a new QC turn.") from exc
    if current != row:
        raise ValueError("The rendered file changed after this turn was queued. Start a new QC turn.")
    path = rendered_path(_parent(ctx), target)
    return row, path


def _frame(value: Any, maximum: int, name: str) -> int:
    if type(value) is not int or not 0 <= value < maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum - 1}.")
    return value


def _record(ctx: ToolContext, data: dict) -> None:
    append_jsonl(ctx.ws.memory / "qc_inspections.jsonl", {"ts": now_iso(), "actor": ctx.agent, **data})


def qc_view(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    row, path = _target(ctx, args["target"])
    frame = _frame(args.get("frame", 0), row["frames"], "frame")
    pixels = read_frames(path, [frame])[frame]
    _target(ctx, args["target"])
    images = [jpeg_bytes(pixels)]
    region = args.get("region")
    receipt = {"target": row["target"], "sha256": row["sha256"], "frames": [frame],
               "width": pixels.shape[1], "height": pixels.shape[0], "region": region,
               "author_model": row.get("author_model"), "coverage": "one frame"}
    if region is not None:
        import math
        if (not isinstance(region, list) or len(region) != 4
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in region)
                or min(region) < 0 or min(region[2:]) <= 0
                or region[0] + region[2] > 1 or region[1] + region[3] > 1):
            raise ValueError("region must be normalized [x, y, width, height] inside the image.")
        height, width = pixels.shape[:2]
        box = [region[0] * width, region[1] * height, region[2] * width, region[3] * height]
        images.append(jpeg_bytes(crop(pixels, box)))
        receipt["crop_pixels"] = box
    _record(ctx, receipt)
    return ToolResult.json(receipt, images=images, artifacts=[row["sha256"]])


def qc_frames(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    row, path = _target(ctx, args["target"])
    f0 = _frame(args.get("f0", 0), row["frames"], "f0")
    f1 = args["f1"]
    if type(f1) is not int or not f0 < f1 <= row["frames"]:
        raise ValueError("f1 is exclusive and must be after f0 within the video.")
    count = args.get("count", 8)
    if type(count) is not int or not 2 <= count <= 12:
        raise ValueError("Choose 2–12 frames per inspection window.")
    frames = sorted({round(f0 + (f1 - f0 - 1) * i / (count - 1)) for i in range(count)})
    pixels = read_frames(path, frames)
    _target(ctx, args["target"])
    if set(pixels) != set(frames):
        raise ValueError("The decoder did not return every requested frame.")
    sheet = contact_sheet([(f"frame {f} · {f / row['fps']:.3f}s", pixels[f]) for f in frames], cols=3, cell_width=480)
    receipt = {"target": row["target"], "sha256": row["sha256"], "frames": frames,
               "f0": f0, "f1": f1, "coverage": "sampled frames only", "sample_count": len(frames)}
    _record(ctx, receipt)
    return ToolResult.json(receipt, images=[jpeg_bytes(sheet)], artifacts=[row["sha256"]])


def qc_items_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    # Roles other than QC can only read snapshots the human explicitly sent.
    audience = "qc" if ctx.role == "qc_lead" and "qc_parent" in ctx.state else "editor"
    store = QCStore(_parent(ctx))
    if args.get("id"):
        return ToolResult.json({"item": store.get(args["id"], audience=audience)})
    return ToolResult.json({"items": store.list_items(audience=audience)})


def qc_item_create(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.role != "qc_lead" or "qc_parent" not in ctx.state:
        raise ValueError("Only the isolated QC conversation can record private findings.")
    row, _ = _target(ctx, args["target"])
    payload = {**args, "pin": {key: row[key] for key in ("sha256", "canvas_file_sha256")}}
    item = QCStore(_parent(ctx)).create(payload, actor="qc_lead", model=ctx.state["qc_model"])
    return ToolResult.json({"item": item}, artifacts=[item["id"]])


_region = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
TOOLS = [
    Tool("qc_targets", "List frozen rendered video versions available for this QC turn.", schema({}), qc_targets, "qc.inspect", "qc"),
    Tool("qc_view", "Inspect one pinned frame plus an optional native crop. Region is normalized [x,y,w,h]. Returns both full context and crop; evidence covers only this frame.",
         schema({"target": {"type": "string"}, "frame": {"type": "integer"}, "region": _region}, ["target", "frame"]), qc_view, "qc.inspect", "qc"),
    Tool("qc_frames", "Inspect 2–12 numbered frames in an explicit half-open range [f0,f1). This is sparse sampling, not whole-window verification.",
         schema({"target": {"type": "string"}, "f0": {"type": "integer"}, "f1": {"type": "integer"}, "count": {"type": "integer"}}, ["target", "f0", "f1"]), qc_frames, "qc.inspect", "qc"),
    Tool("qc_items_read", "Read QC items. The editor receives only immutable snapshots explicitly sent by the person; audit-only items and QC chat are excluded.",
         schema({"id": {"type": "string"}}), qc_items_read, "qc.read", "qc"),
    Tool("qc_item_create", "Record an observation anchored to a pinned video version. Priority is a recommendation, not a release flag. Only the person can hand it to the editor or resolve it.",
         schema({"target": {"type": "string"}, "f0": {"type": "integer"}, "f1": {"type": "integer"}, "region": _region,
                 "expected": {"type": "string"}, "observed": {"type": "string"}, "category": {"type": "string"},
                 "priority": {"type": "string", "enum": ["note", "warning", "blocker"]},
                 "lane": {"type": "string", "enum": ["shared", "audit"]}}, ["target", "f0", "f1", "expected", "observed"]),
         qc_item_create, "qc.findings", "qc"),
]
