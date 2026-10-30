"""Bounded, coordinate-preserving image inspection for every agent host."""

from __future__ import annotations

import io
import math
from typing import Any

from PIL import Image

from vwb.media.frames import fit_image
from vwb.media.images import MAX_SOURCE_PIXELS, load_image
from vwb.tools.base import Tool, ToolContext, ToolResult, schema

MAX_EDGE = 1568  # matches Registry's image bound, so coordinate metadata stays accurate


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def image_inspect(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    image, reference, lineage = load_image(ctx.ws, args["source"])
    edge = int(_number(args.get("max_edge", 1280), "max_edge", 64, MAX_EDGE))
    zoom = _number(args.get("zoom", 1), "zoom", 0.1, 8)
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
    result = {"source": reference, "lineage": lineage, "source_size": [width, height], "crop": [x0, y0, x1 - x0, y1 - y0],
              "display_size": list(shown), "requested_zoom": zoom,
              "display_to_source": {"scale_x": (x1 - x0) / shown[0], "scale_y": (y1 - y0) / shown[1],
                                    "offset_x": x0, "offset_y": y0},
              "coordinate_space": "EXIF-oriented source pixels; x_source = x_display * scale_x + offset_x",
              "note": "Inspection only. Zoom interpolates pixels; it does not recover missing detail."}
    return ToolResult.json(result, images=[encoded])


TOOLS = [Tool("image_inspect", "Inspect a raster image using attachment:<id> (pinned original), a work-relative path, or slot:<name>@<revision>. Optional crop [x,y,w,h] "
              "uses source pixels; zoom enlarges that region up to a bounded display size. Returns the image, "
              "original dimensions and exact display-to-source coordinates. Use media_frame for video frames.",
              schema({"source": {"type": "string"}, "crop": {"type": "array", "items": {"type": "number"},
                                                              "minItems": 4, "maxItems": 4},
                      "zoom": {"type": "number", "minimum": 0.1, "maximum": 8},
                      "max_edge": {"type": "integer", "minimum": 64, "maximum": MAX_EDGE}}, ["source"]),
              image_inspect, "media.inspect", "media")]
