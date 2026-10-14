"""The sunburst image model (slot img.sunburst): create, extract, inpaint, outpaint.

  create    new elements (illustrations, icons, objects, textures, backgrounds), with a real alpha
            channel when asked, optionally guided by reference images (source frames, crops, assets)
  extract   one element of an image as a cut-out on transparency, kept as it appears
  inpaint   regenerate only the masked pixels (remove text or objects, clean a plate, repair)
  outpaint  extend an image to a new size or aspect around where it is placed

Code, not the model, decides what survives: inpaint and outpaint put the original pixels back
everywhere outside the mask (masked edits also alter nearby pixels and text), and every result
is measured (seam, fidelity, text, alpha) before it is registered.
"""

from __future__ import annotations

import base64
import io
import time
from typing import Any

import cv2
import numpy as np
from PIL import Image

from vwb.gateway.core import Gateway, SlotCall
from vwb.gen import qa

SLOT = "img.sunburst"
NO_TEXT = (" Draw no text, letters, numbers, labels, captions, watermarks or logos anywhere in the image; text is "
           "typeset separately.")
MIN_PX, MAX_PX, MAX_EDGE = 655_360, 8_294_400, 3840


def valid_size(w: float, h: float, long_edge: int | None = None) -> tuple[int, int]:
    """The nearest size the model accepts: multiples of 16, aspect 1:3 to 3:1, total pixels and edge in range."""
    aspect = min(3.0, max(1 / 3, w / h))
    le = long_edge or max(w, h)
    W, H = (le, le / aspect) if aspect >= 1 else (le * aspect, le)
    for _ in range(40):
        W16, H16 = max(16, int(round(W / 16)) * 16), max(16, int(round(H / 16)) * 16)
        px = W16 * H16
        if px < MIN_PX:
            W, H = W * 1.06, H * 1.06
        elif px > MAX_PX or max(W16, H16) > MAX_EDGE:
            W, H = W / 1.06, H / 1.06
        else:
            return W16, H16
    raise ValueError(f"no valid model size near {w}x{h}")


def _png(rgba: np.ndarray, name: str = "image.png") -> tuple[str, bytes, str]:
    buf = io.BytesIO()
    Image.fromarray(rgba).save(buf, format="PNG")
    return (name, buf.getvalue(), "image/png")


def _decode(b64: str) -> np.ndarray:
    rgba = np.asarray(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")).copy()
    a = rgba[..., 3]
    rgba[..., 3] = np.where(a >= 250, 255, a)          # the model writes opaque as 254
    return rgba


def _resize(img: np.ndarray, w: int, h: int) -> np.ndarray:
    if img.shape[1] == w and img.shape[0] == h:
        return img
    interp = cv2.INTER_AREA if w * h < img.shape[0] * img.shape[1] else cv2.INTER_CUBIC
    return cv2.resize(img, (w, h), interpolation=interp)


def _call(gw: Gateway, purpose: str, op: str, request: dict[str, Any], fn) -> SlotCall:
    """One image call with retries and the slot's fallbacks; returns RGBA arrays in .raw."""

    def call(cfg: dict[str, Any]) -> SlotCall:
        t0 = time.time()
        sig = {"op": op, "model": cfg["model"], **{k: v for k, v in request.items() if k != "images"}}
        try:
            res = fn(cfg)
        except Exception as exc:
            gw.record(slot_name=SLOT, provider="openai", model=cfg["model"], purpose=purpose, request=sig,
                      response_text="", usage=None, seconds=time.time() - t0, ok=False, error=str(exc))
            raise
        imgs = [_decode(d.b64_json) for d in res.data]
        usage = res.usage.model_dump() if getattr(res, "usage", None) else {}
        gw.record(slot_name=SLOT, provider="openai", model=cfg["model"], purpose=purpose, request=sig,
                  response_text=f"{len(imgs)} image(s)", usage=usage, seconds=time.time() - t0, ok=True)
        return SlotCall(slot=SLOT, provider="openai", model=cfg["model"], text="", usage=usage, raw=imgs)

    return gw.with_fallbacks(SLOT, call)


# ---------------------------------------------------------------- create

def create(gw: Gateway, prompt: str, size: tuple[int, int], *, transparent: bool = True, refs: list[np.ndarray] | None = None,
           quality: str | None = None, n: int = 1, allow_text: bool = False, purpose: str = "sunburst.create") -> SlotCall:
    W, H = valid_size(*size)
    text = prompt.strip() + ("" if allow_text else NO_TEXT)
    if transparent:
        text += " Isolate the subject on a fully transparent background (no backdrop, floor, frame or shadow box)."
    req = {"prompt": text, "size": f"{W}x{H}", "transparent": transparent, "n": n, "refs": len(refs or [])}

    def fn(cfg: dict[str, Any]):
        common = {"model": cfg["model"], "prompt": text, "size": f"{W}x{H}", "quality": quality or cfg.get("quality", "high"),
                  "output_format": "png", "background": "transparent" if transparent else "opaque", "n": n}
        if refs:
            files = [_png(r, f"ref{i}.png") for i, r in enumerate(refs[:16])]
            return gw.openai().images.edit(image=files, timeout=600, **common)
        return gw.openai().images.generate(timeout=600, **common)

    return _call(gw, purpose, "create", req, fn)


# ---------------------------------------------------------------- extract

def extract(gw: Gateway, image: np.ndarray, subject: str, box: list[float] | None = None, *, quality: str | None = None,
            purpose: str = "sunburst.extract") -> dict[str, Any]:
    """Cut one element out of `image` (RGBA). With box, the model sees that region plus context.
    Returns the cut-out (tight to its alpha), where it sat in `image`, and fidelity measures."""
    H, W = image.shape[:2]
    if box:
        x, y, w, h = box
        m = 0.12 * max(w, h)
        x0, y0 = int(max(0, x - m)), int(max(0, y - m))
        x1, y1 = int(min(W, x + w + m)), int(min(H, y + h + m))
    else:
        x0, y0, x1, y1 = 0, 0, W, H
    region = image[y0:y1, x0:x1]
    rw, rh = region.shape[1], region.shape[0]
    mw, mh = valid_size(rw, rh, long_edge=max(1024, min(2048, max(rw, rh))))
    model_in = _resize(region, mw, mh)
    where = (f" It is the element inside the box x={int(x - x0)}..{int(x - x0 + w)}, y={int(y - y0)}..{int(y - y0 + h)} of "
             f"this {rw}x{rh} image." if box else "")
    text = (f"Extract only {subject} from this image as a cut-out on a fully transparent background.{where} Keep it exactly "
            "as it appears: the same shapes, lines, colours, proportions, and the same position and size in the frame. "
            "Remove everything else. Do not redraw, restyle, sharpen, complete or add anything.")

    def fn(cfg: dict[str, Any]):
        return gw.openai().images.edit(model=cfg["model"], image=[_png(model_in)], prompt=text, size=f"{mw}x{mh}",
                                       quality=quality or cfg.get("quality", "high"), background="transparent",
                                       output_format="png", timeout=600)

    call = _call(gw, purpose, "extract", {"prompt": text, "size": f"{mw}x{mh}"}, fn)
    cut = _resize(call.raw[0], rw, rh)
    stats = qa.alpha_stats(cut)
    bb = stats["bbox"] or [0, 0, rw, rh]
    pad = 4
    bx0, by0 = max(0, bb[0] - pad), max(0, bb[1] - pad)
    bx1, by1 = min(rw, bb[0] + bb[2] + pad), min(rh, bb[1] + bb[3] + pad)
    tight = np.ascontiguousarray(cut[by0:by1, bx0:bx1])
    fid = qa.fidelity(tight, image)                       # registered against the whole input image
    return {"rgba": tight, "placed": fid.get("placed"), "fidelity": fid, "alpha": qa.alpha_stats(tight),
            "model": call.model, "usage": call.usage, "prompt": text, "source_region": [x0, y0, rw, rh],
            "region": region, "cut_full": cut}


def _difference_key(region: np.ndarray, bg: np.ndarray, core_dist: float = 60.0, clear_dist: float = 8.0
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Alpha and foreground colour of a subject on a flat background colour, without fringes.

    Pixels far from the background are solid; the colour of those solid pixels is propagated
    outward to the edge pixels, and each edge pixel's alpha is its position on the line from the
    background colour to that local foreground colour. Edge pixels take the local foreground colour,
    so no colour is invented by un-mixing (dividing by a small alpha amplifies error into halos)."""
    img = region.astype(np.float32)
    dist = np.max(np.abs(img - bg), axis=2)
    core = dist > core_dist
    if not core.any():
        return np.zeros(dist.shape, np.float32), img
    fill = (~core).astype(np.uint8) * 255
    local = cv2.inpaint(np.ascontiguousarray(region[..., :3]), fill, 3, cv2.INPAINT_TELEA).astype(np.float32)
    d = local - bg
    alpha = np.clip(((img - bg) * d).sum(axis=2) / np.maximum((d * d).sum(axis=2), 1.0), 0.0, 1.0)
    alpha[core] = 1.0
    alpha[dist < clear_dist] = 0.0
    fg = np.where(alpha[..., None] > 0.98, img, local)
    return alpha, fg


def matte_extract(image: np.ndarray, box: list[float], *, pad: float = 0.04,
                  preserve_rgb: bool = False) -> dict[str, Any]:
    """Deterministic cut-out that keeps the source pixels: a background key when the region sits on a
    flat colour, otherwise GrabCut seeded by the box. Provenance class 'segmented'."""
    from vwb.index.layout import background_color
    H, W = image.shape[:2]
    x, y, w, h = (float(v) for v in box)
    m = pad * max(w, h)
    x0, y0, x1, y1 = int(max(0, x - m)), int(max(0, y - m)), int(min(W, x + w + m)), int(min(H, y + h + m))
    region = np.ascontiguousarray(image[y0:y1, x0:x1, :3])
    source_alpha = image[y0:y1, x0:x1, 3] if image.shape[2] == 4 else np.full(region.shape[:2], 255, np.uint8)
    bg, uniform = background_color(region, band=0.04)
    if preserve_rgb and np.any(source_alpha < 255):
        alpha, fg = source_alpha.astype(np.float32) / 255.0, region.astype(np.float32)
        method = 'existing alpha'
    elif uniform > 0.85:
        alpha, fg = _difference_key(region, bg.astype(np.float32))
        method = "flat-background key"
    else:
        mask = np.zeros(region.shape[:2], np.uint8)
        rh, rw = region.shape[:2]
        if min(rh, rw) < 5:
            raise ValueError('Draw a larger subject region with some background around its edge.')
        rx, ry = max(1, min(rw - 2, int(x - x0))), max(1, min(rh - 2, int(y - y0)))
        rect = (rx, ry, max(1, min(int(w), rw - rx - 1)), max(1, min(int(h), rh - ry - 1)))
        bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.grabCut(cv2.cvtColor(region, cv2.COLOR_RGB2BGR), mask, rect, bgd, fgd, 6, cv2.GC_INIT_WITH_RECT)
        hard = np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.float32)
        alpha = cv2.GaussianBlur(hard, (0, 0), 1.2)
        fg = region.astype(np.float32)
        method = "grabcut"
    alpha = np.minimum(alpha, source_alpha.astype(np.float32) / 255.0)
    if preserve_rgb:
        fg = region.astype(np.float32)
    rgba = np.dstack([np.clip(fg, 0, 255), alpha * 255.0]).astype(np.uint8)
    # Keep low-alpha edge pixels too. QA's bbox uses a visibility threshold,
    # which is unsuitable for a lossless crop of an existing alpha channel.
    ys, xs = np.nonzero(rgba[..., 3] > 0)
    bb = ([int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
          if len(xs) else [0, 0, rgba.shape[1], rgba.shape[0]])
    tight = np.ascontiguousarray(rgba[bb[1]:bb[1] + bb[3], bb[0]:bb[0] + bb[2]])
    return {"rgba": tight, "placed": [x0 + bb[0], y0 + bb[1], bb[2], bb[3]], "method": method,
            "alpha": qa.alpha_stats(tight), "uniform_background": round(uniform, 3)}


# ---------------------------------------------------------------- inpaint

def inpaint(gw: Gateway, image: np.ndarray, mask: np.ndarray, prompt: str, *, quality: str | None = None, tries: int = 1,
            feather: int = 4, purpose: str = "sunburst.inpaint", exact_mask: bool = False) -> dict[str, Any]:
    """Regenerate the pixels where mask > 0 (image RGB or RGBA, mask HxW). The model sees a context
    window around the mask; outside the (feathered) mask the result is the original, exactly."""
    H, W = image.shape[:2]
    ys, xs = np.nonzero(mask > 0)
    if xs.size == 0:
        raise ValueError("the mask is empty")
    bx0, by0, bx1, by1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    m = int(max(64, 0.35 * max(bx1 - bx0, by1 - by0)))
    x0, y0, x1, y1 = max(0, bx0 - m), max(0, by0 - m), min(W, bx1 + m), min(H, by1 + m)
    crop = np.ascontiguousarray(image[y0:y1, x0:x1, :3])
    cm = (mask[y0:y1, x0:x1] > 0).astype(np.uint8)
    if not exact_mask:
        cm = cv2.dilate(cm, np.ones((2 * feather + 1, 2 * feather + 1), np.uint8))
    cw, ch = crop.shape[1], crop.shape[0]
    mw, mh = valid_size(cw, ch, long_edge=max(1024, min(2048, max(cw, ch))))
    model_img = _resize(crop, mw, mh)
    mask_rgba = np.zeros((mh, mw, 4), np.uint8)
    mask_rgba[..., 3] = np.where(_resize(cm * 255, mw, mh) > 0, 0, 255)       # alpha 0 = edit here
    text = prompt.strip() + " Change only the masked area and make it continue its surroundings seamlessly." + NO_TEXT

    def fn(cfg: dict[str, Any]):
        return gw.openai().images.edit(model=cfg["model"], image=[_png(model_img)], mask=_png(mask_rgba, "mask.png"),
                                       prompt=text, size=f"{mw}x{mh}", quality=quality or cfg.get("quality", "high"),
                                       output_format="png", n=max(1, min(tries, 4)), timeout=600)

    call = _call(gw, purpose, "inpaint", {"prompt": text, "size": f"{mw}x{mh}", "tries": tries}, fn)
    soft = cv2.GaussianBlur(cm.astype(np.float32), (0, 0), feather / 2 + 0.5) * cm
    best = None
    for cand in call.raw:
        gen = _resize(cand[..., :3], cw, ch)
        s = qa.seam(gen, crop, keep=1 - cm)
        if best is None or s["mae"] < best[1]["mae"]:
            best = (gen, s)
    gen, seam = best
    comp = (gen.astype(np.float32) * soft[..., None] + crop.astype(np.float32) * (1 - soft[..., None])).astype(np.uint8)
    out = image[..., :3].copy()
    out[y0:y1, x0:x1] = comp
    mb = (bx0 - x0, by0 - y0, bx1 - x0, by1 - y0)
    residue = qa.read_text(comp[mb[1]:mb[3], mb[0]:mb[2]]) if (mb[2] - mb[0]) > 8 and (mb[3] - mb[1]) > 8 else []
    return {"rgb": out, "seam": seam, "text_in_mask_after": residue, "window": [int(x0), int(y0), int(cw), int(ch)],
            "mask_px": int((mask > 0).sum()), "model": call.model, "usage": call.usage, "prompt": text,
            "candidates": len(call.raw)}


# ---------------------------------------------------------------- outpaint

def outpaint(gw: Gateway, image: np.ndarray, size: tuple[int, int], place: list[float], prompt: str, *,
             quality: str | None = None, tries: int = 1, blend: int = 32, purpose: str = "sunburst.outpaint") -> dict[str, Any]:
    """Extend `image` to a canvas of `size` (W, H): it is fitted into `place` [x, y, w, h] and the model
    fills everything around it. The placed pixels come back exactly, except a `blend` band along the
    edges that meet generated pixels, where the two cross-fade to hide the seam (blend=0: exact)."""
    W, H = size
    x, y, w, h = place
    k = min(w / image.shape[1], h / image.shape[0])
    nw, nh = max(1, round(image.shape[1] * k)), max(1, round(image.shape[0] * k))
    ox, oy = int(round(x + (w - nw) / 2)), int(round(y + (h - nh) / 2))
    placed = _resize(image[..., :3], nw, nh)
    canvas = np.zeros((H, W, 4), np.uint8)
    keep = np.zeros((H, W), np.float32)
    x0, y0, x1, y1 = max(0, ox), max(0, oy), min(W, ox + nw), min(H, oy + nh)
    canvas[y0:y1, x0:x1, :3] = placed[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
    canvas[y0:y1, x0:x1, 3] = 255
    keep[y0:y1, x0:x1] = 1.0
    mw, mh = valid_size(W, H, long_edge=max(1024, min(2048, max(W, H))))
    model_img = _resize(canvas, mw, mh)
    mask_rgba = np.zeros((mh, mw, 4), np.uint8)
    mask_rgba[..., 3] = np.where(_resize((keep * 255).astype(np.uint8), mw, mh) > 127, 255, 0)
    text = (prompt.strip() + " Extend the image into the transparent areas so it continues naturally. Keep the existing "
            "part exactly where it is, at the same size; do not move, scale or redraw it." + NO_TEXT)

    def fn(cfg: dict[str, Any]):
        return gw.openai().images.edit(model=cfg["model"], image=[_png(model_img)], mask=_png(mask_rgba, "mask.png"),
                                       prompt=text, size=f"{mw}x{mh}", quality=quality or cfg.get("quality", "high"),
                                       output_format="png", n=max(1, min(tries, 4)), timeout=600)

    call = _call(gw, purpose, "outpaint", {"prompt": text, "size": f"{mw}x{mh}", "tries": tries}, fn)
    orig = np.zeros((H, W, 3), np.uint8)
    orig[y0:y1, x0:x1] = canvas[y0:y1, x0:x1, :3]
    best = None
    for cand in call.raw:
        gen = _resize(cand[..., :3], W, H)
        s = qa.seam(gen, orig, keep=keep)
        if best is None or s["mae"] < best[1]["mae"]:
            best = (gen, s)
    gen, seam = best
    w_keep = keep.copy()
    exact = [x0, y0, x1 - x0, y1 - y0]
    if blend > 0:
        # distance (inside the kept box) to an edge that borders generated pixels; canvas edges do not count
        inner = np.zeros((H + 2, W + 2), np.uint8)
        inner[1:-1, 1:-1] = (keep > 0.5).astype(np.uint8)
        inner[0, :], inner[-1, :], inner[:, 0], inner[:, -1] = inner[1, :], inner[-2, :], inner[:, 1], inner[:, -2]
        dist = cv2.distanceTransform(inner, cv2.DIST_L2, 5)[1:-1, 1:-1]
        w_keep = np.clip(dist / blend, 0.0, 1.0) * (keep > 0.5)
        ex0 = x0 + (blend if x0 > 0 else 0)
        ey0 = y0 + (blend if y0 > 0 else 0)
        ex1 = x1 - (blend if x1 < W else 0)
        ey1 = y1 - (blend if y1 < H else 0)
        exact = [ex0, ey0, max(0, ex1 - ex0), max(0, ey1 - ey0)]
    out = (gen.astype(np.float32) * (1 - w_keep[..., None]) + orig.astype(np.float32) * w_keep[..., None]).astype(np.uint8)
    out[keep > 0.5] = np.where(w_keep[keep > 0.5][..., None] >= 1.0, orig[keep > 0.5], out[keep > 0.5])
    return {"rgb": out, "seam": seam, "kept_box": [x0, y0, x1 - x0, y1 - y0], "exact_box": exact, "blend": blend,
            "model": call.model, "usage": call.usage,
            "prompt": text, "candidates": len(call.raw), "text_outside": qa.read_text(np.where(keep[..., None] > 0, 255, out).astype(np.uint8))}
