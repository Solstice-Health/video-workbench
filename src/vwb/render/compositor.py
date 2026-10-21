"""Deterministic compositor for vwb.canvas/2: canvas -> RGB frames -> x264.

Layers are drawn bottom to top, exactly as the canvas lists them:
  video  the time-mapped source frame, cropped (keyframed), placed in its box (keyframed)
  image  a registry still (with its alpha) fitted into its box
  clip   a registry moving image (generated or keyed, with its alpha) on its own clock
  plate  a run of text, caption and shape layers, rendered once as an alpha plate (Remotion)
Video, image and clip layers share mask (rect, rounded, circle, ellipse), effects (blur, tint,
dim, shadow, border), rotation and opacity.
The same code renders sampled frames for previews and for the verifier's attestation.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from vwb.canvas.anim import active, value_at
from vwb.canvas.compile import Step, plan
from vwb.canvas.model import Canvas, ClipLayer, Effects, ImageLayer, Mask, Sheen, VideoLayer
from vwb.gen import matting
from vwb.media.decode import FrameReader, read_frames
from vwb.util.procs import ToolError, which
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry

COMPOSITOR_VERSION = "2.1"


def hex_rgba(value: str | None) -> tuple[int, int, int, int]:
    v = (value or "#000000").lstrip("#")
    if len(v) == 6:
        v += "FF"
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4, 6))  # type: ignore[return-value]


@lru_cache(maxsize=256)
def mask_alpha(w: int, h: int, shape: str, radius: int) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    if shape == "circle":
        cv2.circle(m, (w // 2, h // 2), min(w, h) // 2, 255, -1, lineType=cv2.LINE_AA)
        return m
    if shape == "ellipse":
        cv2.ellipse(m, (w // 2, h // 2), (w // 2, h // 2), 0, 0, 360, 255, -1, lineType=cv2.LINE_AA)
        return m
    r = max(0, min(radius if shape == "rounded" else 0, min(w, h) // 2))
    if r == 0:
        m[:] = 255
        return m
    cv2.rectangle(m, (r, 0), (w - r - 1, h - 1), 255, -1)
    cv2.rectangle(m, (0, r), (w - 1, h - r - 1), 255, -1)
    for cx, cy in ((r, r), (w - r - 1, r), (r, h - r - 1), (w - r - 1, h - r - 1)):
        cv2.circle(m, (cx, cy), r, 255, -1, lineType=cv2.LINE_AA)
    return m


def alpha_over(dst: np.ndarray, src_rgb: np.ndarray, alpha: np.ndarray, x: int, y: int) -> None:
    """Composite src (h, w, 3) with alpha (h, w) 0-255 onto dst at (x, y), clipped to dst."""
    H, W = dst.shape[:2]
    h, w = src_rgb.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    s = src_rgb[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.uint16)
    a = alpha[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.uint16)[..., None]
    d = dst[y0:y1, x0:x1].astype(np.uint16)
    dst[y0:y1, x0:x1] = ((s * a + d * (255 - a) + 127) // 255).astype(np.uint8)


def alpha_blend_rgba(dst: np.ndarray, src_rgb: np.ndarray, src_a: np.ndarray) -> np.ndarray:
    """Porter-Duff 'over' of an RGB+alpha source onto an RGBA destination of the same size."""
    sa = src_a.astype(np.float32)[..., None] / 255.0
    da = dst[..., 3:4].astype(np.float32) / 255.0
    oa = sa + da * (1 - sa)
    rgb = (src_rgb.astype(np.float32) * sa + dst[..., :3].astype(np.float32) * da * (1 - sa)) / np.maximum(oa, 1e-6)
    return np.dstack([np.clip(rgb, 0, 255), np.clip(oa * 255, 0, 255)]).astype(np.uint8)


class PlateReader:
    """Sequential RGBA frames from an alpha plate (ProRes 4444)."""

    def __init__(self, path: Path, width: int, height: int, fps: float):
        self.path, self.w, self.h, self.fps = str(path), width, height, fps

    def frames(self, start: int = 0, count: int | None = None) -> Iterator[np.ndarray]:
        cmd = [which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error"]
        if start:
            cmd += ["-ss", f"{(start - 0.5) / self.fps:.6f}"]
        cmd += ["-i", self.path]
        if count is not None:
            cmd += ["-frames:v", str(count)]
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{self.w}x{self.h}", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self.w * self.h * 8)
        assert proc.stdout is not None
        size = self.w * self.h * 4
        try:
            while True:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                yield np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 4)
        finally:
            proc.stdout.close()
            proc.kill()
            proc.wait()


class ClipFrames:
    """Every frame of a clip asset as RGBA, decoded once into a memory-mapped cache file.

    Random access is a slice of the map, so previews, renders and loops cost nothing extra.
    Clips without alpha (plain MP4) get an opaque alpha channel."""

    def __init__(self, path: Path, cache_dir: Path, sha256: str):
        cache_dir.mkdir(parents=True, exist_ok=True)
        meta_path, data_path = cache_dir / f"{sha256[:24]}.json", cache_dir / f"{sha256[:24]}.rgba"
        if not meta_path.exists():
            info = json.loads(subprocess.run(
                [which("ffprobe"), "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                 "stream=width,height,r_frame_rate,nb_read_packets", "-of", "json", str(path)],
                capture_output=True, text=True, check=True).stdout)["streams"][0]
            w, h = int(info["width"]), int(info["height"])
            num, den = (int(x) for x in info["r_frame_rate"].split("/"))
            tmp = data_path.with_suffix(".part")
            with tmp.open("wb") as fh:
                subprocess.run([which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
                                "-f", "rawvideo", "-pix_fmt", "rgba", "-"], stdout=fh, check=True)
            n = tmp.stat().st_size // (w * h * 4)
            tmp.rename(data_path)
            meta_path.write_text(json.dumps({"w": w, "h": h, "fps": num / den, "n": n}))
        meta = json.loads(meta_path.read_text())
        self.w, self.h, self.fps, self.n = meta["w"], meta["h"], meta["fps"], meta["n"]
        self.data = np.memmap(data_path, dtype=np.uint8, mode="r", shape=(self.n, self.h, self.w, 4))

    def at(self, l: ClipLayer, f: int, out_fps: float) -> np.ndarray:
        t = l.clip_in + l.rate * (f - l.start) / out_fps
        i = max(0, int(math.floor(t * self.fps + 1e-6)))
        i = i % self.n if l.loop else min(i, self.n - 1)
        return np.asarray(self.data[i])


def fit_rgba(rgba: np.ndarray, iw: int, ih: int, fit: str, align: str, valign: str) -> tuple[np.ndarray, int, int]:
    """Scale an RGBA image into a box: contain (whole image, aligned) or cover (fills, cropped by alignment).
    Returns the tile and its offset inside the box."""
    h, w = rgba.shape[:2]
    scale = (min if fit == "contain" else max)(iw / w, ih / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    img = cv2.resize(rgba, (nw, nh), interpolation=interp) if (nw, nh) != (w, h) else rgba
    fx = {"left": 0.0, "center": 0.5, "right": 1.0}[align]
    fy = {"top": 0.0, "middle": 0.5, "bottom": 1.0}[valign]
    if fit == "cover":
        x0, y0 = int(round((nw - iw) * fx)), int(round((nh - ih) * fy))
        return np.ascontiguousarray(img[y0:y0 + ih, x0:x0 + iw]), 0, 0
    return img, int(round((iw - nw) * fx)), int(round((ih - nh) * fy))


def grade(rgb: np.ndarray, e: Effects) -> np.ndarray:
    """Blur, tint and dim, applied to colour only."""
    if e.blur > 0:
        rgb = cv2.GaussianBlur(rgb, (0, 0), e.blur)
    if (e.tint and e.tint_amount) or e.dim:
        t = rgb.astype(np.float32)
        if e.tint and e.tint_amount:
            r, g, b, _ = hex_rgba(e.tint)
            t = t * (1 - e.tint_amount) + np.array([r, g, b], np.float32) * e.tint_amount
        if e.dim:
            t = t * (1.0 - e.dim)
        rgb = np.clip(t, 0, 255).astype(np.uint8)
    return rgb


def apply_sheen(rgb: np.ndarray, sheen: Sheen, f: int) -> np.ndarray:
    """A soft light band crossing the tile at output frame f (eased in and out), or rgb unchanged outside the
    sweep. Only colour changes; the caller's alpha keeps the light inside the layer's shape."""
    phase = f - sheen.start
    if phase < 0 or (sheen.repeat == 0 and phase >= sheen.frames):
        return rgb
    if sheen.repeat:
        phase %= sheen.repeat
        if phase >= sheen.frames:
            return rgb
    h, w = rgb.shape[:2]
    t = phase / max(1, sheen.frames - 1)
    t = 3 * t * t - 2 * t * t * t                                     # ease in and out
    th = math.radians(sheen.angle)
    xs = np.arange(w, dtype=np.float32)[None, :] / max(1, w - 1)
    ys = np.arange(h, dtype=np.float32)[:, None] / max(1, w - 1)       # same scale on both axes: the tilt is true
    u = xs * math.cos(th) + ys * math.sin(th)
    lo, hi = min(0.0, math.sin(th) * h / max(1, w - 1)), math.cos(th) + max(0.0, math.sin(th) * h / max(1, w - 1))
    c = (lo - sheen.width) + t * (hi - lo + 2 * sheen.width)
    band = sheen.strength * np.exp(-0.5 * ((u - c) / (sheen.width / 2.355)) ** 2)
    r, g, b, _ = hex_rgba(sheen.color)
    out = rgb.astype(np.float32)
    out += (np.array([r, g, b], np.float32) - out) * band[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def rotate_rgba(rgba: np.ndarray, degrees: float) -> tuple[np.ndarray, int, int]:
    """Rotate clockwise about the centre on an expanded transparent canvas; returns the image and the
    shift of its top-left corner relative to the unrotated one."""
    h, w = rgba.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), -degrees, 1.0)
    c, s_ = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(math.ceil(h * s_ + w * c)), int(math.ceil(h * c + w * s_))
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    out = cv2.warpAffine(rgba, M, (nw, nh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    return out, (w - nw) // 2, (h - nh) // 2


class Compositor:
    def __init__(self, ws: Workspace, canvas: Canvas, skip_plates: bool = False):
        self.ws, self.c = ws, canvas
        self.W, self.H = canvas.size
        self.steps: list[Step] = plan(canvas, Registry(ws.assets))
        self.skip_plates = skip_plates
        self.media = {s.ref: s.path for s in ws.load().sources}
        reg = Registry(ws.assets)
        self.images: dict[str, np.ndarray] = {}
        self.clips: dict[str, ClipFrames] = {}
        self._fitted: dict[tuple[str, int, int], tuple[np.ndarray, int, int]] = {}
        self.mattes: dict[tuple[str, int, int], np.ndarray] = {}
        self.media_size: dict[str, tuple[int, int]] = {}
        shas = {s.ref: s.sha256 for s in ws.load().sources}
        for st in self.steps:
            for l in st.layers:
                if isinstance(l, ImageLayer):
                    self.images[l.id] = np.asarray(Image.open(reg.get(l.asset)["path"]).convert("RGBA"))
                elif isinstance(l, ClipLayer):
                    row = reg.get(l.asset)
                    self.clips[l.id] = ClipFrames(Path(row["path"]), ws.plates / "clips", row["sha256"])
                elif isinstance(l, VideoLayer) and l.matte == "person":
                    for span in canvas.time_map:
                        if span.media == l.source and (span.media, span.src_in, span.src_out) not in self.mattes:
                            arr = matting.matte_span(Path(self.media[span.media]), shas[span.media], span.src_in,
                                                     span.src_out, ws.plates / "mattes",
                                                     faces=matting.faces_by_frame(self._index(), span.src_in, span.src_out))
                            self.mattes[(span.media, span.src_in, span.src_out)] = arr
                            self.media_size[span.media] = (arr.shape[1] * 2, arr.shape[2] * 2)

    def _index(self):
        from vwb.index.store import IndexStore
        return IndexStore(self.ws.index / "index.sqlite")

    def plate_path(self, step: Step) -> Path:
        return self.ws.plates / f"{step.plate_id}.mov"

    # ------------------------------------------------------------------ video layers
    @staticmethod
    def _window(l: VideoLayer, f: int, sw: int, sh: int, bw: int, bh: int) -> tuple[float, float, float, float]:
        cx, cy, cw, ch = ((0.0, 0.0, float(sw), float(sh)) if l.crop == "full"
                          else (float(v) for v in value_at(l.crop, f)))
        if l.fit == "cover":
            want = bw / bh
            if cw / ch > want:
                nw = ch * want
                cx, cw = cx + (cw - nw) / 2, nw
            else:
                nh = cw / want
                cy, ch = cy + (ch - nh) / 2, nh
        return cx, cy, cw, ch

    def _alpha_tile(self, alpha: np.ndarray, l: VideoLayer, f: int, bw: int, bh: int) -> np.ndarray:
        """The person matte of a source frame through the same window as the colour tile."""
        sh, sw = alpha.shape[:2]
        cx, cy, cw, ch = self._window(l, f, sw, sh, bw, bh)
        sx, sy = bw / cw, bh / ch
        M = np.array([[sx, 0, 0.5 * sx - sx * cx - 0.5], [0, sy, 0.5 * sy - sy * cy - 0.5]], dtype=np.float64)
        return cv2.warpAffine(alpha, M, (bw, bh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    def _matte(self, media: str, sf: int) -> np.ndarray | None:
        for (m, f0, f1), arr in self.mattes.items():
            if m == media and f0 <= sf < f1:
                h, w = self.media_size[media]
                return matting.full_alpha(arr[sf - f0], (w, h))
        return None

    def _tile(self, src: np.ndarray, l: VideoLayer, f: int, bw: int, bh: int) -> np.ndarray:
        sh, sw = src.shape[:2]
        cx, cy, cw, ch = self._window(l, f, sw, sh, bw, bh)
        e = l.effects
        if e.blur >= 4:
            # a heavy blur is a backdrop: work small, then scale up
            k = 256 / max(cw, 1.0)
            region = src[int(max(0, cy)):int(cy + ch), int(max(0, cx)):int(cx + cw)]
            small = cv2.resize(region, (256, max(1, int(round(ch * k)))), interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (0, 0), max(0.5, e.blur * 256 / bw))
            tile = cv2.resize(small, (bw, bh), interpolation=cv2.INTER_LINEAR)
        else:
            sx, sy = bw / cw, bh / ch
            if sx < 0.75:
                pre = cv2.resize(src, None, fx=sx, fy=sy, interpolation=cv2.INTER_AREA)
                M = np.array([[1, 0, -cx * sx], [0, 1, -cy * sy]], dtype=np.float64)
                tile = cv2.warpAffine(pre, M, (bw, bh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            else:
                M = np.array([[sx, 0, 0.5 * sx - sx * cx - 0.5], [0, sy, 0.5 * sy - sy * cy - 0.5]], dtype=np.float64)
                interp = cv2.INTER_LANCZOS4 if sx > 1.0 else cv2.INTER_CUBIC
                tile = cv2.warpAffine(src, M, (bw, bh), flags=interp, borderMode=cv2.BORDER_REPLICATE)
            if e.blur > 0:
                tile = cv2.GaussianBlur(tile, (0, 0), e.blur)
        if (e.tint and e.tint_amount) or e.dim:
            t = tile.astype(np.float32)
            if e.tint and e.tint_amount:
                r, g, b, _ = hex_rgba(e.tint)
                t = t * (1 - e.tint_amount) + np.array([r, g, b], np.float32) * e.tint_amount
            if e.dim:
                t = t * (1.0 - e.dim)
            tile = np.clip(t, 0, 255).astype(np.uint8)
        return tile

    def _place(self, frame: np.ndarray, tile: np.ndarray, x: int, y: int, mask: Mask, opacity: float, effects) -> None:
        h, w = tile.shape[:2]
        alpha = mask_alpha(w, h, mask.shape, int(round(mask.radius)))
        if effects.shadow:
            s = effects.shadow
            pad = int(s.blur * 2)
            sa = np.zeros((h + 2 * pad, w + 2 * pad), np.uint8)
            sa[pad:pad + h, pad:pad + w] = alpha
            sa = cv2.GaussianBlur(sa, (0, 0), max(1.0, s.blur / 2))
            sa = (sa.astype(np.float32) * s.opacity * opacity).astype(np.uint8)
            alpha_over(frame, np.zeros((sa.shape[0], sa.shape[1], 3), np.uint8), sa, x - pad, y - pad + int(s.dy))
        if effects.border and effects.border.width > 0:
            bw = int(round(effects.border.width))
            r, g, b, _ = hex_rgba(effects.border.color)
            radius = int(round(mask.radius + bw)) if mask.shape == "rounded" else 0
            outer = mask_alpha(w + 2 * bw, h + 2 * bw, mask.shape, radius)
            ring = np.full((h + 2 * bw, w + 2 * bw, 3), (r, g, b), np.uint8)
            alpha_over(frame, ring, (outer.astype(np.float32) * opacity).astype(np.uint8), x - bw, y - bw)
        if mask.shape == "rect" and opacity >= 0.999:
            H, W = frame.shape[:2]
            x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
            if x1 > x0 and y1 > y0:
                frame[y0:y1, x0:x1] = tile[y0 - y:y1 - y, x0 - x:x1 - x]
            return
        if opacity < 0.999:
            alpha = (alpha.astype(np.float32) * opacity).astype(np.uint8)
        alpha_over(frame, tile, alpha, x, y)

    def _place_rgba(self, frame: np.ndarray, rgba: np.ndarray, x: int, y: int, l, opacity: float, f: int) -> None:
        """Sheen, mask, rotate, shadow, border and composite a graded RGBA tile whose top-left is (x, y)."""
        rgb, alpha = rgba[..., :3], rgba[..., 3]
        if l.effects.sheen:
            rgb = apply_sheen(rgb, l.effects.sheen, f)
        h, w = alpha.shape
        if l.mask.shape != "rect" or l.mask.radius:
            alpha = np.minimum(alpha, mask_alpha(w, h, l.mask.shape, int(round(l.mask.radius))))
        border = l.effects.border if (l.effects.border and l.effects.border.width > 0) else None
        if border:
            bw = int(round(border.width))
            r, g, b, _ = hex_rgba(border.color)
            radius = int(round(l.mask.radius + bw)) if l.mask.shape == "rounded" else 0
            ring = mask_alpha(w + 2 * bw, h + 2 * bw, l.mask.shape, radius).copy()
            ring[bw:bw + h, bw:bw + w] = np.maximum(0, ring[bw:bw + h, bw:bw + w].astype(np.int16) - alpha).astype(np.uint8)
            canvas = np.zeros((h + 2 * bw, w + 2 * bw, 4), np.uint8)
            canvas[..., :3] = (r, g, b)
            canvas[..., 3] = ring
            canvas[bw:bw + h, bw:bw + w] = alpha_blend_rgba(canvas[bw:bw + h, bw:bw + w], rgb, alpha)
            rgb, alpha, x, y = canvas[..., :3], canvas[..., 3], x - bw, y - bw
        deg = float(value_at(l.rotate, f))
        if abs(deg) > 0.01:
            rot, dx, dy = rotate_rgba(np.dstack([rgb, alpha]), deg)
            rgb, alpha, x, y = rot[..., :3], rot[..., 3], x + dx, y + dy
        if l.effects.shadow:
            s = l.effects.shadow
            pad = int(s.blur * 2)
            sa = np.zeros((alpha.shape[0] + 2 * pad, alpha.shape[1] + 2 * pad), np.uint8)
            sa[pad:pad + alpha.shape[0], pad:pad + alpha.shape[1]] = alpha
            sa = cv2.GaussianBlur(sa, (0, 0), max(1.0, s.blur / 2))
            sa = (sa.astype(np.float32) * s.opacity * opacity).astype(np.uint8)
            alpha_over(frame, np.zeros((sa.shape[0], sa.shape[1], 3), np.uint8), sa, x - pad, y - pad + int(s.dy))
        if opacity < 0.999:
            alpha = (alpha.astype(np.float32) * opacity).astype(np.uint8)
        alpha_over(frame, np.ascontiguousarray(rgb), np.ascontiguousarray(alpha), x, y)

    def _place_fitted(self, frame: np.ndarray, rgba: np.ndarray, l, bx: float, by: float, iw: int, ih: int,
                      opacity: float, f: int, cache: bool = False) -> None:
        key = (l.id, iw, ih)
        hit = self._fitted.get(key) if cache else None
        if hit is None:
            tile, ox, oy = fit_rgba(rgba, iw, ih, l.fit, l.align, l.valign)
            tile = np.dstack([grade(np.ascontiguousarray(tile[..., :3]), l.effects), tile[..., 3]])
            hit = (tile, ox, oy)
            if cache:
                if len(self._fitted) > 256:
                    self._fitted.clear()
                self._fitted[key] = hit
        tile, ox, oy = hit
        self._place_rgba(frame, tile, int(round(bx)) + ox, int(round(by)) + oy, l, opacity, f)

    # ------------------------------------------------------------------ one frame
    def compose(self, f: int, src: np.ndarray, plates: dict[str, np.ndarray | None],
                where: tuple[str, int] | None = None) -> np.ndarray:
        frame = np.zeros((self.H, self.W, 3), np.uint8)
        for st in self.steps:
            if st.kind == "plate":
                plate = plates.get(st.plate_id or "") if st.start <= f < st.end else None
                if plate is None:
                    continue
                a = plate[..., 3]
                rows = np.flatnonzero(a.max(axis=1))
                if rows.size:
                    cols = np.flatnonzero(a[rows[0]:rows[-1] + 1].max(axis=0))
                    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
                    alpha_over(frame, np.ascontiguousarray(plate[y0:y1, x0:x1, :3]),
                               np.ascontiguousarray(a[y0:y1, x0:x1]), int(x0), int(y0))
                continue
            l = st.layers[0]
            if not active(l.span, f):
                continue
            opacity = float(value_at(l.opacity, f))
            if opacity <= 0.001:
                continue
            bx, by, bw, bh = (float(v) for v in value_at(l.box, f))
            iw, ih = max(1, int(round(bw))), max(1, int(round(bh)))
            if isinstance(l, VideoLayer):
                tile = self._tile(src, l, f, iw, ih)
                alpha = self._matte(*where) if (l.matte == "person" and where) else None
                if alpha is not None:
                    rgba = np.dstack([tile, self._alpha_tile(alpha, l, f, iw, ih)])
                    self._place_rgba(frame, rgba, int(round(bx)), int(round(by)), l, opacity, f)
                elif abs(float(value_at(l.rotate, f))) > 0.01 or l.effects.sheen:
                    rgba = np.dstack([tile, np.full(tile.shape[:2], 255, np.uint8)])
                    self._place_rgba(frame, rgba, int(round(bx)), int(round(by)), l, opacity, f)
                else:
                    self._place(frame, tile, int(round(bx)), int(round(by)), l.mask, opacity, l.effects)
            elif isinstance(l, ImageLayer):
                self._place_fitted(frame, self.images[l.id], l, bx, by, iw, ih, opacity, f, cache=True)
            elif isinstance(l, ClipLayer):
                self._place_fitted(frame, self.clips[l.id].at(l, f, self.c.fps), l, bx, by, iw, ih, opacity, f)
        return frame

    # ------------------------------------------------------------------ streams
    def _source_frames(self) -> Iterator[tuple[int, tuple[str, int] | None, np.ndarray]]:
        if not self.c.time_map:
            blank = np.zeros((1, 1, 3), np.uint8)
            for f in range(self.c.length):
                yield f, None, blank
            return
        out = 0
        for span in self.c.time_map:
            for sf, rgb in FrameReader(self.media[span.media]).frames(span.src_in, span.src_out):
                yield out, (span.media, sf), rgb
                out += 1

    def frames(self) -> Iterator[tuple[int, np.ndarray]]:
        readers = {} if self.skip_plates else {
            st.plate_id: PlateReader(self.plate_path(st), self.W, self.H, self.c.fps).frames()
            for st in self.steps if st.kind == "plate"}
        ranges = {st.plate_id: (st.start, st.end) for st in self.steps if st.kind == "plate"}
        for f, where, src in self._source_frames():
            plates = {pid: next(it, None) for pid, it in readers.items() if ranges[pid][0] <= f < ranges[pid][1]}
            yield f, self.compose(f, src, plates, where)

    def sample(self, out_frames: list[int], stills: dict[str, dict[int, np.ndarray]] | None = None) -> dict[int, np.ndarray]:
        """Render chosen output frames (previews and the verifier's attestation). `stills` gives plate
        frames directly ({plate_id: {output_frame: rgba}}) instead of reading rendered plates."""
        out_frames = sorted({int(f) for f in out_frames if 0 <= f < self.c.length})
        where: dict[int, tuple[str, int]] = {}
        acc = 0
        for span in self.c.time_map:
            n = span.src_out - span.src_in
            for f in out_frames:
                if acc <= f < acc + n:
                    where[f] = (span.media, span.src_in + (f - acc))
            acc += n
        by_media: dict[str, list[int]] = {}
        for m, sf in where.values():
            by_media.setdefault(m, []).append(sf)
        decoded = {m: read_frames(self.media[m], idx) for m, idx in by_media.items()}
        result = {}
        for f in out_frames:
            plates: dict[str, np.ndarray | None] = {}
            if stills is not None:
                plates = {pid: frames[f] for pid, frames in stills.items() if f in frames}
            elif not self.skip_plates:
                for st in self.steps:
                    if st.kind == "plate" and st.start <= f < st.end and self.plate_path(st).exists():
                        plates[st.plate_id or ""] = next(PlateReader(self.plate_path(st), self.W, self.H, self.c.fps)
                                                         .frames(start=f - st.start, count=1), None)
            if f in where:
                m, sf = where[f]
                result[f] = self.compose(f, decoded[m][sf], plates, (m, sf))
            else:
                result[f] = self.compose(f, np.zeros((1, 1, 3), np.uint8), plates)
        return result


def encode(comp: Compositor, out_path: Path, profile: dict, threads: int = 8) -> Path:
    """Composite every frame and encode once with x264 (BT.709, yuv420p, CFR)."""
    W, H, fps = comp.W, comp.H, comp.c.fps
    v = profile["video"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-vf", "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,format=yuv420p",
           "-c:v", "libx264", "-preset", v.get("preset", "slow"), "-crf", str(v.get("crf", 17)),
           "-profile:v", v.get("profile", "high"), "-g", str(int(fps * v.get("gop_s", 2))), "-bf", "2",
           "-threads", str(threads), "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-color_range", "tv", "-movflags", "+faststart", "-an", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    n = 0
    try:
        for _, frame in comp.frames():
            proc.stdin.write(frame.tobytes())
            n += 1
    finally:
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        proc.wait()
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        raise ToolError(f"x264 encode failed: {err[-1500:]}")
    if n != comp.c.length:
        raise ToolError(f"compositor produced {n} frames; the canvas declares {comp.c.length}")
    return out_path
