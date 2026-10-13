"""Frame extraction, crops, PNG encoding and labelled contact sheets."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from vwb.media.decode import read_frames

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def crop(rgb: np.ndarray, box: Sequence[float] | None) -> np.ndarray:
    if not box:
        return rgb
    h, w = rgb.shape[:2]
    x, y, bw, bh = (int(round(v)) for v in box)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + bw), min(h, y + bh)
    return rgb[y0:y1, x0:x1]


def resize_to_width(rgb: np.ndarray, width: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    if w == width:
        return rgb
    img = Image.fromarray(rgb).resize((width, max(1, round(h * width / w))), Image.LANCZOS)
    return np.asarray(img)


def png_bytes(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def jpeg_bytes(rgb: np.ndarray, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def fit_image(data: bytes, max_edge: int = 1568, max_bytes: int = 600_000) -> bytes:
    """Bound an image sent to a model: models downscale past ~1.5k px anyway, and agent hosts cap
    message sizes. Returns the input unchanged when it already fits."""
    img = Image.open(io.BytesIO(data))
    if max(img.size) <= max_edge and len(data) <= max_bytes:
        return data
    img = img.convert("RGB")
    if max(img.size) > max_edge:
        scale = max_edge / max(img.size)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    for quality in (88, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= max_bytes:
            break
    return buf.getvalue()


def save_png(rgb: np.ndarray, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(path)
    return path


def extract(path: Path | str, frame: int, box: Sequence[float] | None = None, width: int | None = None) -> np.ndarray:
    rgb = read_frames(path, [frame])[frame]
    rgb = crop(rgb, box)
    if width:
        rgb = resize_to_width(rgb, width)
    return rgb


def draw_boxes(rgb: np.ndarray, boxes: Sequence[tuple[Sequence[float], str]], color=(255, 40, 40), width: int = 3) -> np.ndarray:
    img = Image.fromarray(rgb.copy())
    draw = ImageDraw.Draw(img)
    font = _font(max(14, rgb.shape[1] // 80))
    for box, label in boxes:
        x, y, w, h = box
        draw.rectangle([x, y, x + w, y + h], outline=color, width=width)
        if label:
            draw.text((x + 4, y + 2), label, fill=color, font=font)
    return np.asarray(img)


def contact_sheet(cells: Sequence[tuple[str, np.ndarray]], cols: int = 4, cell_width: int = 360,
                  pad: int = 6, bg=(24, 24, 28), label_color=(255, 255, 255)) -> np.ndarray:
    """Grid of labelled frames. Cells may differ in aspect; each row takes its tallest cell."""
    if not cells:
        return np.zeros((10, 10, 3), np.uint8)
    font = _font(max(12, cell_width // 22))
    label_h = font.size + 6 if hasattr(font, "size") else 18
    resized = [(label, resize_to_width(img, cell_width)) for label, img in cells]
    rows = [resized[i:i + cols] for i in range(0, len(resized), cols)]
    row_heights = [max(img.shape[0] for _, img in row) + label_h for row in rows]
    width = cols * cell_width + (cols + 1) * pad
    height = sum(row_heights) + (len(rows) + 1) * pad
    sheet = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(sheet)
    y = pad
    for row, rh in zip(rows, row_heights):
        x = pad
        for label, img in row:
            sheet.paste(Image.fromarray(img), (x, y + label_h))
            draw.text((x + 2, y + 1), label, fill=label_color, font=font)
            x += cell_width + pad
        y += rh + pad
    return np.asarray(sheet)
