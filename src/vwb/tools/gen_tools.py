"""Generative tools: the sunburst image model and the Omni video model.

Agents use these to rebuild a source's elements for a new frame: cut an element out with its
alpha, regenerate a masked area (remove text, clean a plate), extend an image to a new aspect,
create a new element on transparency, or animate an element (first/last frame, reference
images, a key-colour plate keyed back to alpha). Every result lands in the asset registry with
its lineage and measurements; the canvas cites it (image or clip layer), and the verifier
raises the provenance flags (GEN-CREATED, GEN-RECOVERED, GEN-TEXT, SYNTH-PERSON, MATTE-RISK)
for every output that uses it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from vwb.gen import images as sb
from vwb.gen import key as keyer
from vwb.gen import qa
from vwb.gen import video as omni
from vwb.gen.refs import resolve, save_png
from vwb.index.ocr import agree_key, similar
from vwb.index.store import IndexStore
from vwb.media.decode import read_frames
from vwb.media.frames import contact_sheet, jpeg_bytes
from vwb.tools.base import Tool, ToolContext, ToolResult, parse_json_arg, schema
from vwb.util.hashing import new_id
from vwb.workspace.registry import Registry

REF = {"type": "string", "description": "t:<seconds> or f:<frame> (source frame), or slot:<name>@<rev> (asset; "
                                        "slot:<name>@<rev>#t=<s> for a frame of a clip)"}
BOX = {"type": "array", "items": {"type": "number"}, "description": "[x, y, w, h] in the pixels of what it applies to"}
QUALITY = {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]}


def _slot(a: dict[str, Any], prefix: str) -> str:
    name = (a.get("slot") or "").strip().removeprefix("slot:").split("@")[0]
    return name or new_id(prefix, 3)


def _register(ctx: ToolContext, arr_or_path: np.ndarray | Path, *, slot: str, kind: str, receipt: str,
              derived_from: list[dict[str, Any]], fit: dict[str, Any], notes: str = "") -> str:
    reg = Registry(ctx.ws.assets)
    if isinstance(arr_or_path, Path):
        return reg.register(arr_or_path, slot=slot, kind=kind, receipt=receipt, derived_from=derived_from, fit=fit, notes=notes)
    with tempfile.TemporaryDirectory(dir=ctx.ws.assets) as tmp:
        path = save_png(arr_or_path, Path(tmp) / f"{slot}.png")
        return reg.register(path, slot=slot, kind=kind, receipt=receipt, derived_from=derived_from, fit=fit, notes=notes)


def _source_text(region: np.ndarray) -> list[str]:
    try:
        return qa.read_text(region)
    except Exception:  # noqa: BLE001 - OCR is evidence, not a gate
        return []


def _text_compare(found: list[str], source: list[str]) -> dict[str, Any]:
    """Do two OCR readings say the same thing? Tolerant of reading order and small OCR noise."""
    if not found and not source:
        return {"found": [], "source": [], "match": True}
    f = " ".join(sorted(agree_key(t) for t in found))
    s = " ".join(sorted(agree_key(t) for t in source))
    score = similar(f, s)
    return {"found": found, "source": source, "similarity": round(score, 3), "match": score >= 0.9}


# ---------------------------------------------------------------- sunburst

def sunburst_create(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    refs = [resolve(ctx.ws, r["ref"] if isinstance(r, dict) else r, (r.get("crop") if isinstance(r, dict) else None))
            for r in parse_json_arg(a.get("refs") or [])]
    size = a.get("size") or [1024, 1024]
    n = max(1, min(int(a.get("n", 1)), 4))
    call = sb.create(ctx.gateway, a["prompt"], (float(size[0]), float(size[1])), transparent=a.get("transparent", True),
                     refs=[r.rgba for r in refs], quality=a.get("quality"), n=n, allow_text=bool(a.get("allow_text")))
    base = _slot(a, "gen")
    out, previews = [], []
    for i, img in enumerate(call.raw):
        text, n_faces = _source_text(img), qa.faces(img)
        fit = {"native_size": [img.shape[1], img.shape[0]], "alpha": qa.alpha_stats(img), "text": {"found": text},
               "faces": n_faces, "model": call.model, "prompt": a["prompt"], "op": "create"}
        ref = _register(ctx, img, slot=base if n == 1 else f"{base}-{i + 1}", kind="image", receipt="sunburst.create",
                        derived_from=[r.lineage for r in refs], fit=fit, notes=a.get("note", ""))
        out.append({"ref": ref, "size": fit["native_size"], "alpha": fit["alpha"], "text_found": text, "faces": n_faces})
        previews.append(img)
    warn = [f"{o['ref']}: the model drew text {o['text_found']}; generated text is flagged GEN-TEXT" for o in out if o["text_found"]]
    warn += [f"{o['ref']}: {o['faces']} face(s); a generated person is flagged SYNTH-PERSON" for o in out if o["faces"]]
    return ToolResult.json({"assets": out, "model": call.model, "warnings": warn,
                            "use": "Place it with an image layer (asset=ref). Provenance: generated (GEN-CREATED)."},
                           images=[jpeg_bytes(qa.preview(previews, height=560))], artifacts=[o["ref"] for o in out])


def sunburst_extract(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    src = resolve(ctx.ws, a["source"], a.get("crop"))
    box = a.get("box")
    method = a.get("method", "sunburst")
    slot = _slot(a, "cut")
    if method == "matte":
        if not box:
            return ToolResult.error("method matte needs a box")
        res = sb.matte_extract(src.rgba, box, preserve_rgb=bool(a.get('preserve_rgb')))
        receipt, extra = "matte.extract", {"method": res["method"], "uniform_background": res["uniform_background"]}
        region = src.rgba[res["placed"][1]:res["placed"][1] + res["placed"][3], res["placed"][0]:res["placed"][0] + res["placed"][2]]
        shown = [region, res["rgba"]]
    else:
        if not a.get("subject"):
            return ToolResult.error("say what to extract (subject), for example 'the lungs illustration'")
        res = sb.extract(ctx.gateway, src.rgba, a["subject"], box, quality=a.get("quality"))
        receipt, extra = "sunburst.extract", {"fidelity": res["fidelity"], "model": res["model"], "prompt": res["prompt"]}
        region = res["region"]
        shown = [region, res["cut_full"]]
    cx, cy = (src.lineage.get("crop") or [0, 0])[:2]              # placements are in the pixels of the whole source
    if res.get("placed"):
        res["placed"] = [res["placed"][0] + cx, res["placed"][1] + cy, res["placed"][2], res["placed"][3]]
    if method == "matte":
        text = {"found": None, "match": True, "note": "source pixels kept; text is the source's own"}
    elif res["placed"]:
        px, py, pw, ph = (int(v) for v in res["placed"])
        px, py = px - cx, py - cy                                       # back into the (cropped) image we hold
        footprint = src.rgba[max(0, py):py + ph, max(0, px):px + pw]      # only the text the element itself covers
        text = _text_compare(_source_text(res["rgba"]), _source_text(footprint))
    else:
        found = _source_text(res["rgba"])
        text = {"found": found, "match": not found, "note": "not registered to the source; any text is unverified"}
    fit = {"native_size": [res["rgba"].shape[1], res["rgba"].shape[0]], "alpha": res["alpha"], "text": text,
           "faces": qa.faces(res["rgba"]), "placed_in_source": res["placed"], "placed_frame": "source", "op": "extract",
           "subject": a.get("subject"), **extra}
    ref = _register(ctx, res["rgba"], slot=slot, kind="image", receipt=receipt, derived_from=[src.lineage], fit=fit,
                    notes=a.get("note", ""))
    notes = []
    if method != "matte":
        fid = res["fidelity"]
        if not fid.get("registered"):
            notes.append("the cut-out could not be located in the source: treat it as a redraw, compare the preview")
        elif fid["psnr"] < 24:
            notes.append(f"fidelity {fid['psnr']} dB at scale {fid['scale']}: the model redrew the element noticeably; "
                         "compare the preview, or use method=matte to keep the source pixels")
    if not text["match"]:
        notes.append(f"text differs from the source region ({text['found']} vs {text['source']}): GEN-TEXT")
    return ToolResult.json({"ref": ref, "placed_in_source": res["placed"], "size": fit["native_size"], "alpha": res["alpha"],
                            "text": text, **({"fidelity": res["fidelity"]} if method != "matte" else {"method": res["method"]}),
                            "notes": notes, "preview": "left: the source region; right: the cut-out on a checkerboard",
                            "use": "Place it with an image layer (asset=ref)."},
                           images=[jpeg_bytes(qa.preview(shown, height=560))], artifacts=[ref])


def _mask_from(ctx: ToolContext, a: dict[str, Any], src_ref: str, shape: tuple[int, int]) -> np.ndarray:
    spec = parse_json_arg(a.get("mask") or {})
    H, W = shape
    m = np.zeros((H, W), np.uint8)
    pad = float(spec.get("pad", 6))
    boxes = [list(b) for b in spec.get("boxes", [])]
    if spec.get("text_tracks") or spec.get("blocks"):
        if not src_ref.startswith(("t:", "f:")):
            raise ValueError("text_tracks and blocks come from the index, so the source must be a source frame (t: or f:)")
        st = IndexStore(ctx.ws.index / "index.sqlite")
        for tid in spec.get("text_tracks", []):
            r = st.one("SELECT x, y, w, h FROM text_tracks WHERE id=?", (int(tid),))
            if r:
                boxes.append([r["x"], r["y"], r["w"], r["h"]])
        for bid in spec.get("blocks", []):
            r = st.one("SELECT x, y, w, h FROM blocks WHERE id=?", (int(bid),))
            if r:
                boxes.append([r["x"], r["y"], r["w"], r["h"]])
    for x, y, w, h in boxes:
        x0, y0 = int(max(0, x - pad)), int(max(0, y - pad))
        x1, y1 = int(min(W, x + w + pad)), int(min(H, y + h + pad))
        m[y0:y1, x0:x1] = 1
    if spec.get("asset"):
        alpha = resolve(ctx.ws, spec["asset"]).rgba[..., 3]
        if alpha.shape != (H, W):
            raise ValueError(f"mask asset is {alpha.shape[1]}x{alpha.shape[0]}; the image is {W}x{H}")
        m |= (alpha > 127).astype(np.uint8)
    if not m.any():
        raise ValueError("the mask is empty: give boxes, text_tracks, blocks or a mask asset")
    return m


def sunburst_inpaint(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    src = resolve(ctx.ws, a["source"], a.get("crop"))
    if a.get("crop") and parse_json_arg(a.get("mask") or {}).get("text_tracks"):
        return ToolResult.error("text_tracks/blocks are in source-frame pixels; inpaint the full frame, then crop when placing")
    mask = _mask_from(ctx, a, a["source"], src.rgba.shape[:2])
    res = sb.inpaint(ctx.gateway, src.rgba, mask, a["prompt"], quality=a.get("quality"), tries=int(a.get("tries", 1)),
                     exact_mask=bool(a.get("exact_mask")))
    rgb = res["rgb"]
    rgba = np.dstack([rgb, src.rgba[..., 3]])
    fit = {"native_size": [rgb.shape[1], rgb.shape[0]], "seam": res["seam"], "mask_px": res["mask_px"],
           "text": {"found": res["text_in_mask_after"], "where": "inside the mask after the edit"},
           "model": res["model"], "prompt": a["prompt"], "op": "inpaint", "window": res["window"]}
    ref = _register(ctx, rgba, slot=_slot(a, "fill"), kind="image", receipt="sunburst.inpaint",
                    derived_from=[src.lineage, {"mask": parse_json_arg(a.get("mask"))}], fit=fit, notes=a.get("note", ""))
    x, y, w, h = res["window"]
    overlay = src.rgba[..., :3].copy()
    overlay[mask > 0] = (overlay[mask > 0] * 0.4 + np.array([255, 0, 80]) * 0.6).astype(np.uint8)
    shown = [overlay[y:y + h, x:x + w], rgb[y:y + h, x:x + w]]
    notes = []
    if res["seam"]["mae"] > 12:
        notes.append(f"seam error {res['seam']['mae']}: the model moved pixels near the mask edge; check the preview")
    if res["text_in_mask_after"]:
        notes.append(f"text still read inside the mask: {res['text_in_mask_after']}")
    return ToolResult.json({"ref": ref, "size": fit["native_size"], "seam": res["seam"], "text_in_mask_after": res["text_in_mask_after"],
                            "notes": notes, "kept": "every pixel outside the feathered mask is the original",
                            "preview": "left: mask over the source window; right: result"},
                           images=[jpeg_bytes(qa.preview(shown, height=560))], artifacts=[ref])


def sunburst_outpaint(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    src = resolve(ctx.ws, a["source"], a.get("crop"))
    size = [int(v) for v in a["size"]]
    place = a.get("place") or [0, 0, size[0], size[1]]
    res = sb.outpaint(ctx.gateway, src.rgba, (size[0], size[1]), place, a["prompt"], quality=a.get("quality"),
                      tries=int(a.get("tries", 1)), blend=int(a.get("blend", 32)))
    rgb = res["rgb"]
    fit = {"native_size": size, "seam": res["seam"], "kept_box": res["kept_box"], "exact_box": res["exact_box"],
           "blend": res["blend"], "model": res["model"], "prompt": a["prompt"],
           "op": "outpaint", "text": {"found": res["text_outside"], "where": "in the generated area"}}
    ref = _register(ctx, np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)]), slot=_slot(a, "extend"), kind="image",
                    receipt="sunburst.outpaint", derived_from=[src.lineage], fit=fit, notes=a.get("note", ""))
    notes = []
    if res["seam"]["mae"] > 12:
        notes.append(f"seam error {res['seam']['mae']}: the generated surround does not line up with the kept image")
    if res["text_outside"]:
        notes.append(f"the model drew text in the extension: {res['text_outside']} (GEN-TEXT)")
    return ToolResult.json({"ref": ref, "size": size, "kept_box": res["kept_box"], "exact_box": res["exact_box"],
                            "seam": res["seam"], "notes": notes,
                            "kept": f"source pixels are exact inside exact_box; a {res['blend']} px band cross-fades into "
                                    "the generated surround (blend=0 keeps the whole placed image exact)"},
                           images=[jpeg_bytes(qa.preview([rgb], height=720))], artifacts=[ref])


# ---------------------------------------------------------------- Omni

PLATE = {("9:16", "1080p"): (1080, 1920), ("9:16", "720p"): (720, 1280), ("16:9", "1080p"): (1920, 1080),
         ("16:9", "720p"): (1280, 720)}


def omni_video(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    task = a.get("task", "text")
    aspect, res_ = a.get("aspect", "9:16"), a.get("resolution", "1080p")
    W, H = PLATE.get((aspect, res_), (1080, 1920))
    key_mode = (a.get("key") or "none").lower()

    def element(spec):
        if spec is None:
            return None
        spec = parse_json_arg(spec)
        spec = {"ref": spec} if isinstance(spec, str) else spec
        r = resolve(ctx.ws, spec["ref"], spec.get("crop"))
        return r, spec.get("place")

    first, last = element(a.get("first")), element(a.get("last"))
    refs = [element(r) for r in parse_json_arg(a.get("refs") or [])]
    lineage = [x[0].lineage for x in [first, last, *refs] if x]
    elements = [x[0].rgba for x in [first, last, *refs] if x]
    prompt = a["prompt"].strip()
    key_info: dict[str, Any] = {}
    if key_mode != "none":
        name, rgb, risk = keyer.choose_key(elements, prefer=None if key_mode == "auto" else key_mode)
        key_info = {"key": name, "rgb": list(rgb), "risk": risk}
        if risk[name] > 0.03:
            key_info["warning"] = f"{risk[name]:.0%} of the element's pixels are close to {name}; the key may cut holes"
        hexkey = "#" + "".join(f"{c:02X}" for c in rgb)
        prompt += (f" The background is a perfectly flat, uniform {name} chroma-key colour ({hexkey}) for the whole clip: "
                   "no gradient, shadow, floor, reflection, vignette, particles or texture on it. Keep the subject fully "
                   "inside the frame. No text.")
        bg = rgb
    else:
        bg = tuple(int(a.get("background", "#FFFFFF").lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    def frame_of(x):
        r, place = x
        if r.rgba[..., 3].min() == 255 and place is None:
            return np.ascontiguousarray(r.rgba[..., :3])
        el = r.rgba
        default = [W * 0.15, H * 0.5 - (W * 0.7) * el.shape[0] / el.shape[1] / 2, W * 0.7, (W * 0.7) * el.shape[0] / el.shape[1]]
        return keyer.plate([(el, place or default)], (W, H), bg)

    with tempfile.TemporaryDirectory(dir=ctx.ws.assets) as tmp:
        raw_path = Path(tmp) / "omni.mp4"
        vid = None
        if a.get("video"):
            vid = Path(Registry(ctx.ws.assets).get(a["video"])["path"])
        gen = omni.generate(ctx.gateway, task=task, prompt=prompt, out=raw_path,
                            first=frame_of(first) if first else None, last=frame_of(last) if last else None,
                            refs=[frame_of(r) for r in refs] or None, video=vid, seconds=int(a.get("seconds", 6)),
                            resolution=res_, aspect=aspect, seed=a.get("seed"))
        slot = _slot(a, "motion")
        fit = {"model": gen["model"], "task": gen["task"], "format": gen["format"], "prompt": a["prompt"], "op": "omni",
               "key": key_info or None}
        raw_ref = _register(ctx, raw_path, slot=f"{slot}-raw" if key_info else slot, kind="video", receipt="omni.generate",
                            derived_from=lineage, fit=fit, notes=a.get("note", ""))
        out = {"raw": raw_ref, "model": gen["model"], "seconds_taken": gen["seconds"], **({"key": key_info} if key_info else {})}
        if key_info:
            keyed_path = Path(tmp) / "keyed.mov"
            kres = keyer.key_clip(raw_path, keyed_path, preview_frames=6, erode=float(a.get("erode", 0)),
                                  feather=float(a.get("feather", 0)), expected=tuple(key_info["rgb"]))
            m = kres["metrics"]
            kfit = {**fit, "matte": m, "native_size": m["size"], "faces": qa.faces(kres["preview"][len(kres["preview"]) // 2]),
                    "text": {"found": _source_text(kres["preview"][len(kres["preview"]) // 2])}}
            ref = _register(ctx, keyed_path, slot=slot, kind="video", receipt="omni.key",
                            derived_from=[*lineage, {"kind": "asset", "ref": raw_ref}], fit=kfit, notes=a.get("note", ""))
            out.update(ref=ref, matte=m)
            if m["holes_max"] > 0.01 or m["key_noise_max"] > 3:
                out["notes"] = [f"matte risk: holes {m['holes_max']:.1%}, background noise {m['key_noise_max']}; check the preview"]
            sheet = qa.preview(kres["preview"], height=420)
        else:
            out["ref"] = raw_ref
            idx = [int(i) for i in np.linspace(0, 23 * int(a.get("seconds", 6)), 6)]
            got = read_frames(raw_path, idx)
            sheet = contact_sheet([(f"{i / 24:.1f}s", got[i]) for i in idx if i in got], cols=6, cell_width=220)
    out["use"] = "Place it with a clip layer (asset=ref, start, clip_in, rate, loop). Provenance: generative motion."
    return ToolResult.json(out, images=[jpeg_bytes(sheet)], artifacts=[out["ref"]])


def clip_rekey(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Key a generated clip again from its green-screen original with new matte settings (no new generation)."""
    import json as _json
    reg = Registry(ctx.ws.assets)
    row = reg.get(a["ref"])
    lineage = _json.loads(row["derived_from"])
    if row["receipt"] == "omni.key":
        raw = next((d["ref"] for d in lineage if d.get("kind") == "asset" and reg.get(d["ref"])["receipt"] == "omni.generate"), None)
        keyed_row = row
    elif row["receipt"] == "omni.generate":
        raw, keyed_row = f"slot:{row['slot']}@{row['rev']}", None
    else:
        return ToolResult.error(f"{a['ref']} is not a video-model clip (receipt {row['receipt']})")
    if raw is None:
        return ToolResult.error(f"{a['ref']} has no green-screen original to key again")
    raw_row = reg.get(raw)
    raw_fit = _json.loads(raw_row["fit"])
    if not raw_fit.get("key"):
        return ToolResult.error(f"{raw} was not generated on a key colour (key: none), so it cannot be keyed")
    erode, feather = float(a.get("erode", 0)), float(a.get("feather", 0))
    slot = ((a.get("slot") or "").strip().removeprefix("slot:").split("@")[0]
            or (keyed_row["slot"] if keyed_row else raw_row["slot"].removesuffix("-raw")))   # default: a new revision
    with tempfile.TemporaryDirectory(dir=ctx.ws.assets) as tmp:
        keyed_path = Path(tmp) / "keyed.mov"
        kres = keyer.key_clip(Path(raw_row["path"]), keyed_path, preview_frames=6, erode=erode, feather=feather,
                              expected=tuple(raw_fit["key"]["rgb"]) if raw_fit["key"].get("rgb") else None)
        m = kres["metrics"]
        base = _json.loads(keyed_row["fit"]) if keyed_row else raw_fit
        fit = {**base, "matte": m, "native_size": m["size"], "rekeyed_from": raw}
        src_lineage = [d for d in (_json.loads(keyed_row["derived_from"]) if keyed_row else lineage)
                       if not (d.get("kind") == "asset" and d.get("ref") == raw)]
        ref = _register(ctx, keyed_path, slot=slot, kind="video", receipt="omni.key",
                        derived_from=[*src_lineage, {"kind": "asset", "ref": raw}], fit=fit, notes=a.get("note", ""))
    notes = []
    if m["holes_max"] > 0.01:
        notes.append(f"matte holes {m['holes_max']:.1%}: erosion may have eaten into thin parts; try less")
    return ToolResult.json({"ref": ref, "from": raw, "matte": m, "notes": notes,
                            "preview": "keyed frames on a checkerboard (alpha visible)"},
                           images=[jpeg_bytes(qa.preview(kres["preview"], height=420))], artifacts=[ref])


# ---------------------------------------------------------------- trace

def asset_trace(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from vwb.gen import trace as tracer
    src = resolve(ctx.ws, a["source"], a.get("crop"))
    rgba = src.rgba
    keyed = False
    if rgba[..., 3].min() == 255:                       # an opaque crop: key out its flat background first
        from vwb.index.layout import background_color
        bg, uniform = background_color(rgba[..., :3], band=0.04)
        if uniform < 0.85:
            return ToolResult.error("the crop has no flat background to key out; cut the element out first "
                                    "(sunburst_extract), then trace the cut-out")
        alpha, fg = sb._difference_key(np.ascontiguousarray(rgba[..., :3]), bg.astype(np.float32))
        rgba = np.dstack([np.clip(fg, 0, 255), alpha * 255]).astype(np.uint8)
        keyed = True
    res = tracer.trace(rgba, detail=a.get("detail", "medium"))
    fid = tracer.fidelity(rgba, res["paths"], res["viewbox"])
    vec = {"paths": res["paths"], "viewbox": res["viewbox"]}
    with tempfile.TemporaryDirectory(dir=ctx.ws.assets) as tmp:
        path = Path(tmp) / "vector.json"
        path.write_text(json.dumps(vec))
        fit = {"native_size": [rgba.shape[1], rgba.shape[0]], "viewbox": res["viewbox"], "paths": len(res["paths"]),
               "colours": res["colours"], "fidelity": fid, "op": "trace", "detail": a.get("detail", "medium"),
               "background_keyed": keyed}
        ref = _register(ctx, path, slot=_slot(a, "vec"), kind="vector", receipt="asset.trace",
                        derived_from=[src.lineage], fit=fit, notes=a.get("note", ""))
    big = tracer.rasterize(res["paths"], res["viewbox"], (rgba.shape[1] * 3, rgba.shape[0] * 3))
    notes = []
    if fid["iou"] < 0.9:
        notes.append(f"silhouette overlap {fid['iou']}: shapes changed; try detail=high, or recreate it with sunburst_create")
    if len(res["colours"]) >= 7:
        notes.append("many colours: painterly art traces poorly; recreate it with sunburst_create instead")
    return ToolResult.json({"ref": ref, "paths": len(res["paths"]), "colours": res["colours"], "fidelity": fid, "notes": notes,
                            "use": "A shape layer with shape='svg' and asset=ref; fill='#RRGGBB' recolours every path. "
                                   "It stays sharp at any size."},
                           images=[jpeg_bytes(qa.preview([rgba, big], height=480))], artifacts=[ref])


# ---------------------------------------------------------------- person matte

def video_matte(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from vwb.gen import matting
    src = ctx.ws.load().source()
    fps = float(IndexStore(ctx.ws.index / "index.sqlite").media()["fps"])
    f0, f1 = int(round(float(a["t0"]) * fps)), int(round(float(a["t1"]) * fps))
    if f1 - f0 < 2:
        return ToolResult.error("give a span of at least two frames (t0 < t1, source seconds)")
    store = IndexStore(ctx.ws.index / "index.sqlite")
    arr = matting.matte_span(Path(src.path), src.sha256, f0, f1, ctx.ws.plates / "mattes",
                             faces=matting.faces_by_frame(store, f0, f1))
    st = matting.stats([arr])
    picks = [f0 + int(i * (f1 - f0 - 1) / 5) for i in range(6)]
    rgb = read_frames(src.path, picks)
    shown = []
    for f in picks:
        alpha = matting.full_alpha(arr[f - f0], (rgb[f].shape[1], rgb[f].shape[0]))
        shown.append(np.dstack([rgb[f], alpha]))
    notes = []
    if st["coverage"] < 0.01:
        notes.append("almost no person found in this span")
    if st["flicker"] > 0.02:
        notes.append(f"matte flicker {st['flicker']}: edges may boil; keep the speaker small or add a soft shadow")
    return ToolResult.json({"span_frames": [f0, f1], "coverage": st["coverage"], "flicker": st["flicker"], "notes": notes,
                            "use": "A video layer with matte='person' shows only the people of the source, in sync with "
                                   "the time map (this span is now cached; uncached spans are matted at render)."},
                           images=[jpeg_bytes(qa.preview(shown, height=360))])


# ---------------------------------------------------------------- view

def asset_view(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    row = Registry(ctx.ws.assets).get(a["ref"])
    import json
    fit = json.loads(row["fit"])
    info = {"ref": f"slot:{row['slot']}@{row['rev']}", "kind": row["kind"], "prov": row["prov"], "receipt": row["receipt"],
            "derived_from": json.loads(row["derived_from"]), "fit": fit, "notes": row["notes"]}
    if row["kind"] == "vector":
        from vwb.gen import trace as tracer
        vec = json.loads(Path(row["path"]).read_text())
        vw, vh = vec["viewbox"]
        k = 640 / max(vw, vh)
        img = qa.preview([tracer.rasterize(vec["paths"], vec["viewbox"], (round(vw * k), round(vh * k)))], height=640)
        return ToolResult.json(info, images=[jpeg_bytes(img)])
    if row["kind"] == "video":
        from vwb.media.probe import probe
        p = probe(row["path"], with_keyframes=False)
        n = p.n_frames
        idx = [int(i) for i in np.linspace(0, max(0, n - 1), 6)]
        from vwb.render.compositor import ClipFrames
        clip = ClipFrames(Path(row["path"]), ctx.ws.plates / "clips", row["sha256"])
        frames = [np.asarray(clip.data[min(i, clip.n - 1)]) for i in idx]
        img = qa.preview(frames, height=420)
    else:
        img = qa.preview([resolve(ctx.ws, info["ref"]).rgba], height=640)
    return ToolResult.json(info, images=[jpeg_bytes(img)])


TOOLS = [
    Tool("sunburst_create", "Image model (sunburst): create new elements - illustrations, icons, objects, textures, "
         "backgrounds - with a real alpha channel (transparent=true, default) so they sit on any design. refs: up to 16 "
         "reference images ({ref, crop}) that guide subject, style or colours (for example a source frame showing the "
         "original illustration, to rebuild it larger and cleaner). The tool forbids drawn text (typeset text instead). "
         "size [w, h] (any aspect 1:3 to 3:1; e.g. [1024, 1024], [1024, 1536]). n up to 4 variants. Returns asset refs, "
         "alpha stats, any text or faces found, and a preview.",
         schema({"prompt": {"type": "string"}, "size": {"type": "array", "items": {"type": "number"}},
                 "transparent": {"type": "boolean"}, "refs": {"type": "array", "items": {"type": "object"}},
                 "quality": QUALITY, "n": {"type": "integer"}, "slot": {"type": "string"}, "note": {"type": "string"},
                 "allow_text": {"type": "boolean"}}, ["prompt"]), sunburst_create, "gen.image", "gen"),
    Tool("sunburst_extract", "Cut one element out of a frame or asset as a transparent cut-out: a chart, an illustration, "
         "an icon, a person, a product, a panel. method=sunburst (default): the image model isolates `subject` (box "
         "narrows where it is) and redraws its edges cleanly; fidelity (PSNR against the source pixels) and text "
         "agreement are measured. method=matte: no model, the source pixels are kept and alpha is computed (flat "
         "background key or GrabCut in the box). Returns the asset ref and where it sat in the source (placed_in_source).",
         schema({"source": REF, "box": BOX, "subject": {"type": "string"}, "method": {"type": "string",
                 "enum": ["sunburst", "matte"]}, "crop": BOX, "quality": QUALITY, "slot": {"type": "string"},
                 "preserve_rgb": {"type": "boolean", "description": "For matte: keep original RGB and use existing alpha when present."},
                 "note": {"type": "string"}}, ["source"]), sunburst_extract, "gen.image", "gen"),
    Tool("sunburst_inpaint", "Masked inpaint: regenerate only the masked pixels of a frame or asset, for example to remove "
         "text, footnotes, a logo or an object, or to clean a plate behind an element you lift out. mask: {boxes: "
         "[[x,y,w,h]], text_tracks: [ids], blocks: [ids] (index ids, on source frames), asset: an alpha mask, pad}. "
         "Every pixel outside the feathered mask stays the original; the seam and any text left inside the mask are "
         "measured. exact_mask=true feathers inward and preserves every pixel outside the submitted mask. "
         "tries up to 4 (the best seam wins).",
         schema({"source": REF, "mask": {"type": "object"}, "prompt": {"type": "string"}, "crop": BOX, "tries": {"type": "integer"},
                 "quality": QUALITY, "slot": {"type": "string"}, "note": {"type": "string"},
                 "exact_mask": {"type": "boolean"}}, ["source", "mask", "prompt"]),
         sunburst_inpaint, "gen.image", "gen"),
    Tool("sunburst_outpaint", "Outpaint: extend a frame, crop or asset to a new canvas size or aspect (for example a 16:9 "
         "background or artwork to a full 1080x1920 vertical plate). The image is fitted into `place` [x, y, w, h] on a "
         "canvas of `size` [W, H] and the model fills the rest. The placed pixels stay exact except a `blend` band "
         "(default 32 px) that cross-fades into the surround to hide the seam; blend=0 keeps them all. tries up to 4.",
         schema({"source": REF, "size": {"type": "array", "items": {"type": "integer"}}, "place": BOX, "blend": {"type": "integer"},
                 "prompt": {"type": "string"}, "crop": BOX, "tries": {"type": "integer"}, "quality": QUALITY,
                 "slot": {"type": "string"}, "note": {"type": "string"}}, ["source", "size", "prompt"]),
         sunburst_outpaint, "gen.image", "gen"),
    Tool("omni_video", "Video model (Omni): generate a 3-10 s clip (24 fps, up to 1080x1920). task: text (prompt only); "
         "frames (first and optional last frame: it animates from one to the other - use it to animate a still element "
         "or to move between two compositions); reference (1-3 reference images, cited in the prompt as <IMAGE_REF_0>...; "
         "keeps a subject or style consistent); edit or extend (an existing clip asset). Elements are {ref, crop, place}; "
         "a transparent element is put on a plate at `place` (default: centred, 70% wide). key: none (default), auto, "
         "green, blue or magenta - green-screen prompting: the element goes on a validated key-colour plate, the model "
         "is told to keep the background flat, and the harness keys the result to an alpha clip for a clip layer "
         "(matte holes and noise are measured).",
         schema({"task": {"type": "string", "enum": ["text", "frames", "reference", "edit", "extend"]},
                 "prompt": {"type": "string"}, "first": {"type": "object"}, "last": {"type": "object"},
                 "refs": {"type": "array", "items": {"type": "object"}}, "video": {"type": "string"},
                 "seconds": {"type": "integer"}, "resolution": {"type": "string", "enum": ["720p", "1080p"]},
                 "aspect": {"type": "string", "enum": ["9:16", "16:9"]}, "key": {"type": "string",
                 "enum": ["none", "auto", "green", "blue", "magenta"]}, "background": {"type": "string"},
                 "seed": {"type": "integer"}, "slot": {"type": "string"}, "note": {"type": "string"},
                 "erode": {"type": "number", "description": "keyed clips: pull the matte edge in by this many pixels "
                           "(removes green fringe and spill; 1-4 is typical)"},
                 "feather": {"type": "number", "description": "keyed clips: soften the matte edge by this many pixels"}},
                ["prompt"]),
         omni_video, "gen.video", "gen"),
    Tool("clip_rekey", "Key a video-model clip again from its green-screen original with new matte settings - erode "
         "(pull the edge in by N pixels, removing fringe and spill) and feather (soften the edge) - without generating "
         "it again. ref is the keyed clip or its raw original; the result is a new revision of the keyed slot.",
         schema({"ref": {"type": "string"}, "erode": {"type": "number"}, "feather": {"type": "number"},
                 "slot": {"type": "string"}, "note": {"type": "string"}}, ["ref"]), clip_rekey, "gen.video", "gen"),
    Tool("asset_trace", "Trace a flat icon, logo, pictogram or simple illustration (a cut-out with alpha, from "
         "sunburst_extract or sunburst_create) into colour vector paths (VTracer, colours quantised): it then renders "
         "crisply at any size and can be recoloured, instead of an upscaled blur. Returns a vector asset for a shape layer "
         "(shape='svg'), with silhouette overlap and colour error against the raster. detail: low | medium | high.",
         schema({"source": REF, "crop": BOX, "detail": {"type": "string", "enum": ["low", "medium", "high"]},
                 "slot": {"type": "string"}, "note": {"type": "string"}}, ["source"]), asset_trace, "gen.image", "gen"),
    Tool("video_matte", "Cut the people out of the source footage between t0 and t1 (source seconds) with a video "
         "matting model, cache it and preview it (coverage and flicker measured). Then a video layer with "
         "matte='person' shows the speaker without their background, in sync with the narration, on a designed page.",
         schema({"t0": {"type": "number"}, "t1": {"type": "number"}}, ["t0", "t1"]), video_matte, "gen.image", "gen"),
    Tool("asset_view", "Look at a registry asset: a still on a checkerboard (alpha visible) or six frames of a clip, with "
         "its provenance, lineage and measurements.", schema({"ref": {"type": "string"}}, ["ref"]), asset_view, "tool", "gen"),
]
