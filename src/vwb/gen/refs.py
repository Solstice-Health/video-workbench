"""Image references that tools accept, resolved to RGBA arrays.

  t:<seconds>            a source frame at a time, for example "t:47.2"
  f:<frame>              a source frame by number, for example "f:1416"
  source:<ref>#t=2       a specific registered source at a time (#f=60 for a frame)
  slot:<name>@<rev>      a registry asset (a still; for a clip, its first frame)
  slot:<name>@<rev>#t=2  a frame of a clip asset at a time in the clip
  attachment:<id>       an immutable uploaded image
  work:<relative path>  the current working image (bare paths also accepted)
Each may carry a crop [x, y, w, h] in the pixels of what it names.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from vwb.index.paths import source
from vwb.media.decode import FrameReader, read_frames
from vwb.media.images import load_image
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry


@dataclass
class Resolved:
    rgba: np.ndarray                  # h x w x 4, uint8
    ref: str
    lineage: dict[str, Any] = field(default_factory=dict)   # what it came from, for derived_from


def _rgba(rgb: np.ndarray) -> np.ndarray:
    if rgb.shape[2] == 4:
        return rgb
    return np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])


def _frame(path, coordinate: str) -> int:
    try:
        value = float(coordinate[2:])
    except ValueError:
        raise ValueError("Frame/time coordinates must be finite nonnegative numbers.") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError("Frame/time coordinates must be finite nonnegative numbers.")
    if coordinate.startswith("f:"):
        if not value.is_integer():
            raise ValueError("Frame coordinates must be whole numbers.")
        return int(value)
    return int(round(value * FrameReader(path).fps))


def resolve(ws: Workspace, ref: str, crop: list[float] | None = None) -> Resolved:
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("An image reference is required: attachment:<id>, a work-relative path, or an asset/frame ref.")
    ref = ref.strip()
    if ref.startswith(("t:", "f:", "source:")):
        if ref.startswith("source:"):
            media, sep, at = ref[7:].partition("#")
            if not sep or not at.startswith(("t=", "f=")):
                raise ValueError("Use source:<media ref>#t=<seconds> or #f=<frame>.")
            src = source(ws, media, require_explicit=True)
            coordinate = at.replace("=", ":", 1)
        else:
            src = source(ws, require_explicit=True)
            coordinate = ref
        frame = _frame(src.path, coordinate)
        rgba = _rgba(read_frames(src.path, [frame])[frame])
        lineage = {"kind": "source_frame", "media": src.ref, "frame": frame}
    elif ref.startswith("slot:"):
        body, sep, at = ref.partition("#")
        row = Registry(ws.assets).get(body)
        if row["kind"] == "video":
            if sep and not at.startswith(("t=", "f=")):
                raise ValueError("Use #t=<seconds> or #f=<frame> for a clip asset.")
            frame = _frame(row["path"], at.replace("=", ":", 1) if sep else "f:0")
            rgba = _rgba(read_frames(row["path"], [frame])[frame])
            lineage = {"kind": "asset", "ref": f"slot:{row['slot']}@{row['rev']}", "sha256": row["sha256"],
                       "prov": row["prov"], "frame": frame, **({"t": float(at[2:])} if at.startswith("t=") else {})}
        else:
            if sep:
                raise ValueError("Still-image assets do not have time/frame coordinates.")
            pixels, ref, lineage = load_image(ws, body)
            rgba = np.asarray(pixels)
    else:
        pixels, ref, lineage = load_image(ws, ref)
        rgba = np.asarray(pixels)
    if crop is not None:
        if (not isinstance(crop, list) or len(crop) != 4
                or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in crop)
                or crop[2] <= 0 or crop[3] <= 0):
            raise ValueError("crop must be [x, y, positive width, positive height] in source pixels, using finite numbers.")
        x, y, w, h = crop
        H, W = rgba.shape[:2]
        x0, y0, x1, y1 = max(0, math.floor(x)), max(0, math.floor(y)), min(W, math.ceil(x + w)), min(H, math.ceil(y + h))
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"crop {crop} is outside the {W}x{H} image")
        rgba = rgba[y0:y1, x0:x1]
        lineage["crop"] = [x0, y0, x1 - x0, y1 - y0]
    return Resolved(np.ascontiguousarray(rgba), ref, lineage)


def save_png(rgba: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba).save(path, format="PNG")
    return path
