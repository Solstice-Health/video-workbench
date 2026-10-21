"""Bounded, coordinate-preserving image inspection for every agent host."""

from __future__ import annotations

import io
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from vwb.media.frames import fit_image
from vwb.tools.base import Tool, ToolContext, ToolResult, schema
from vwb.web.workspace_files import open_file
from vwb.workspace.registry import Registry as Assets

MAX_SOURCE_PIXELS = 40_000_000
MAX_SOURCE_BYTES = 50_000_000
MAX_EDGE = 1568  # matches Registry's image bound, so coordinate metadata stays accurate


def _source(ctx: ToolContext, source: str) -> tuple[str, str]:
    if not isinstance(source, str) or not source or len(source) > 4096:
        raise ValueError("source must be a work-relative image path or slot:<name>@<revision>")
    if source.startswith("slot:"):
        row = Assets(ctx.ws.assets).get(source)
        path = Path(row["path"])
        # Keep the original pathname: open_file rejects symlinks through pinned descriptors.
        relative = path.relative_to(ctx.ws.assets).as_posix()
        reference = f"slot:{row['slot']}@{row['rev']}"
        return "assets/" + relative, reference
    if source.startswith("work:"):
        source = source[5:]
    if source.startswith("work/"):
        source = source[5:]
    if Path(source).is_absolute():
        raise ValueError("Use a path relative to the request's work folder")
    return "work/" + source, source


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def image_inspect(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path, reference = _source(ctx, args["source"])
    edge = int(_number(args.get("max_edge", 1280), "max_edge", 64, MAX_EDGE))
    zoom = _number(args.get("zoom", 1), "zoom", 0.1, 8)
    with open_file(ctx.ws, path) as stream, Image.open(stream) as opened:
        if os.fstat(stream.fileno()).st_size > MAX_SOURCE_BYTES:
            raise ValueError(f"Image exceeds the {MAX_SOURCE_BYTES // 1_000_000} MB inspection limit")
        if opened.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"}:
            raise ValueError("Unsupported raster image; export PNG, JPEG or WebP first")
        if opened.width * opened.height > MAX_SOURCE_PIXELS:
            raise ValueError(f"Image exceeds the {MAX_SOURCE_PIXELS // 1_000_000} megapixel inspection limit")
        # Match browsers' EXIF orientation before defining the coordinate space.
        image = ImageOps.exif_transpose(opened).convert("RGBA")
    width, height = image.size
    box = args.get("crop")
    if box is None:
        x0, y0, x1, y1 = 0, 0, width, height
    else:
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError("crop must contain [x, y, width, height] in oriented source pixels")
        x, y, w, h = [_number(value, "crop coordinate", -MAX_SOURCE_PIXELS, MAX_SOURCE_PIXELS)
                      for value in box]
        if w <= 0 or h <= 0:
            raise ValueError("Crop width and height must be positive")
        x0, y0 = max(0, math.floor(x)), max(0, math.floor(y))
        x1, y1 = min(width, math.ceil(x + w)), min(height, math.ceil(y + h))
        if x1 <= x0 or y1 <= y0:
            raise ValueError("Crop does not intersect the image")
    image = image.crop((x0, y0, x1, y1))
    scale = min(zoom, edge / max(image.size))
    shown = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    if image.size != shown:
        image = image.resize(shown, Image.Resampling.LANCZOS)
    data = io.BytesIO()
    image.save(data, format="PNG")
    encoded = fit_image(data.getvalue())
    result = {"source": reference, "source_size": [width, height], "crop": [x0, y0, x1 - x0, y1 - y0],
              "display_size": list(shown), "requested_zoom": zoom,
              "display_to_source": {"scale_x": (x1 - x0) / shown[0], "scale_y": (y1 - y0) / shown[1],
                                    "offset_x": x0, "offset_y": y0},
              "coordinate_space": "EXIF-oriented source pixels; x_source = x_display * scale_x + offset_x",
              "note": "Inspection only. Zoom interpolates pixels; it does not recover missing detail."}
    return ToolResult.json(result, images=[encoded])


TOOLS = [Tool("image_inspect", "Inspect a raster image in work/ or the asset registry. Optional crop [x,y,w,h] "
              "uses source pixels; zoom enlarges that region up to a bounded display size. Returns the image, "
              "original dimensions and exact display-to-source coordinates. Use media_frame for video frames.",
              schema({"source": {"type": "string"}, "crop": {"type": "array", "items": {"type": "number"},
                                                              "minItems": 4, "maxItems": 4},
                      "zoom": {"type": "number", "minimum": 0.1, "maximum": 8},
                      "max_edge": {"type": "integer", "minimum": 64, "maximum": MAX_EDGE}}, ["source"]),
              image_inspect, "media.inspect", "media")]
