"""Image references that tools accept, resolved to RGBA arrays.

  t:<seconds>            a source frame at a time, for example "t:47.2"
  f:<frame>              a source frame by number, for example "f:1416"
  source:<ref>#t=2       a specific registered source at a time (#f=60 for a frame)
  slot:<name>@<rev>      a registry asset (a still; for a clip, its first frame)
  slot:<name>@<rev>#t=2  a frame of a clip asset at a time in the clip
Each may carry a crop [x, y, w, h] in the pixels of what it names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from vwb.index.paths import SourceIndexes, source
from vwb.media.decode import read_frames
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


def resolve(ws: Workspace, ref: str, crop: list[float] | None = None) -> Resolved:
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
        fps = float(SourceIndexes(ws).for_media(src.ref).media()["fps"])
        frame = int(round(float(coordinate[2:]) * fps)) if coordinate.startswith("t:") else int(coordinate[2:])
        rgba = _rgba(read_frames(src.path, [frame])[frame])
        lineage = {"kind": "source_frame", "media": src.ref, "frame": frame}
    elif ref.startswith("slot:"):
        body, _, at = ref.partition("#t=")
        row = Registry(ws.assets).get(body)
        if row["kind"] == "video":
            t = float(at or 0)
            from vwb.media.probe import probe
            fps = probe(row["path"], with_keyframes=False).fps
            frame = int(round(t * fps))
            rgba = _rgba(read_frames(row["path"], [frame])[frame])
        else:
            rgba = np.asarray(Image.open(row["path"]).convert("RGBA"))
        lineage = {"kind": "asset", "ref": f"slot:{row['slot']}@{row['rev']}", "sha256": row["sha256"],
                   "prov": row["prov"], **({"t": float(at)} if at else {})}
    else:
        raise ValueError(f"unknown image reference {ref!r}: use source:<ref>#t=<seconds>, #f=<frame> or slot:<name>@<rev>")
    if crop:
        x, y, w, h = (int(round(v)) for v in crop)
        H, W = rgba.shape[:2]
        x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if x1 - x0 < 4 or y1 - y0 < 4:
            raise ValueError(f"crop {crop} is outside the {W}x{H} image")
        rgba = rgba[y0:y1, x0:x1]
        lineage["crop"] = [x0, y0, x1 - x0, y1 - y0]
    return Resolved(np.ascontiguousarray(rgba), ref, lineage)


def save_png(rgba: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba).save(path, format="PNG")
    return path
