"""Asset lab rungs 0 and 1 for marks (logos, wordmarks).

Rung 0 takes the largest clean instance the index found (spec: "largest, lowest QP,
unoccluded"). Rung 1 fuses every aligned instance of the same scale with a temporal median,
which removes compression noise. Both keep the mark on its own background as a rounded
badge, so no matte is invented (no MATTE-RISK). A keyed variant is also stored.
Every recovered mark still raises NEEDS-SOURCE:logo until a master file is uploaded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from vwb.index.layout import background_color
from vwb.index.store import IndexStore
from vwb.media.decode import read_frames
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry


def _rounded_alpha(w: int, h: int, r: int) -> np.ndarray:
    mask = np.zeros((h, w), np.uint8)
    cv2.rectangle(mask, (r, 0), (w - r - 1, h - 1), 255, -1)
    cv2.rectangle(mask, (0, r), (w - 1, h - r - 1), 255, -1)
    for cx, cy in ((r, r), (w - r - 1, r), (r, h - r - 1), (w - r - 1, h - r - 1)):
        cv2.circle(mask, (cx, cy), r, 255, -1, lineType=cv2.LINE_AA)
    return mask


def _key(crop: np.ndarray) -> tuple[np.ndarray, list[int]]:
    """Background key: alpha from the distance to the border colour, colour un-mixed."""
    bg, uniform = background_color(crop, band=0.06)
    bgf = bg.astype(np.float32)
    dist = np.max(np.abs(crop.astype(np.float32) - bgf), axis=2)
    alpha = np.clip((dist - 10.0) / 60.0, 0.0, 1.0)
    fg = (crop.astype(np.float32) - (1.0 - alpha[..., None]) * bgf) / np.maximum(alpha[..., None], 1e-3)
    fg = np.clip(fg, 0, 255)
    rgba = np.dstack([fg, alpha * 255.0]).astype(np.uint8)
    return rgba, bg.tolist()


def recover_mark(ws: Workspace, mark_id: int, slot: str | None = None, pad: float = 0.18) -> dict[str, Any]:
    store = IndexStore(ws.index / "index.sqlite")
    mark = store.one("SELECT * FROM marks WHERE id=?", (mark_id,))
    if mark is None:
        raise KeyError(f"No mark {mark_id}")
    req = ws.load()
    src = req.source()
    if mark["best_frame"] is not None and mark["best_box"]:
        frame_idx, box = int(mark["best_frame"]), json.loads(mark["best_box"])
        rung = "asset_lab.rung0"
    else:
        frame_idx = int((mark["f0"] + mark["f1"]) // 2)
        box = [mark["x"], mark["y"], mark["w"], mark["h"]]
        rung = "asset_lab.rung0"
    x, y, w, h = box
    px, py = w * pad, h * pad
    x0, y0 = int(max(0, x - px)), int(max(0, y - py))
    m = store.media()
    x1, y1 = int(min(m["width"], x + w + px)), int(min(m["height"], y + h + py))
    # rung 1: fuse neighbouring frames of the same static instance (temporal median)
    neighbours = [frame_idx + d for d in range(-12, 13, 3) if 0 <= frame_idx + d < m["n_frames"]]
    frames = read_frames(src.path, neighbours)
    crops = [frames[i][y0:y1, x0:x1] for i in neighbours if i in frames]
    ref = crops[len(crops) // 2]
    same = [c for c in crops if c.shape == ref.shape and float(np.mean(np.abs(c.astype(np.int16) - ref.astype(np.int16)))) < 6.0]
    if len(same) >= 3:
        crop = np.median(np.stack(same), axis=0).astype(np.uint8)
        rung = "asset_lab.rung1"
    else:
        crop = ref
    slot = slot or f"mark-{(mark['name'] or 'logo').lower().replace(' ', '-')[:40]}"
    tmp_dir = ws.assets / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    hh, ww = crop.shape[:2]
    badge = np.dstack([crop, _rounded_alpha(ww, hh, max(4, int(min(ww, hh) * 0.16)))])
    badge_path = tmp_dir / f"{slot}-badge.png"
    Image.fromarray(badge, "RGBA").save(badge_path)
    keyed, bg = _key(crop)
    keyed_path = tmp_dir / f"{slot}-keyed.png"
    Image.fromarray(keyed, "RGBA").save(keyed_path)
    sharp = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY), cv2.CV_64F).var())
    derived = [{"media": src.ref, "frame": frame_idx, "box": [x0, y0, x1 - x0, y1 - y0], "fused_frames": len(same)}]
    fit = {"native_size": [ww, hh], "sharpness": round(sharp, 1), "bound": None,
           "note": "no bound until X06 reports (spec D16)"}
    reg = Registry(ws.assets)
    badge_ref = reg.register(badge_path, slot=slot, kind="mark.badge", receipt=rung, derived_from=derived, fit=fit,
                             notes=f"mark {mark_id} '{mark['name']}' on its own background, rounded")
    keyed_ref = reg.register(keyed_path, slot=f"{slot}-keyed", kind="mark.keyed", receipt=rung, derived_from=derived,
                             fit={**fit, "key_bg": bg}, notes="background-keyed variant (edges estimated)")
    return {"slot": slot, "badge": badge_ref, "keyed": keyed_ref, "rung": rung, "frame": frame_idx,
            "native_size": [ww, hh], "fused_frames": len(same), "name": mark["name"]}
