"""The third text reader: a vision model reads native-resolution crops of the lines the two OCR
engines disagree on. Two of three readings agreeing settles a line; otherwise all three are kept
and the line stays unverified.

Small text (footnotes, references) is where the OCR engines disagree most, and where agents
are most tempted to paste source pixels instead of retyping. A third reader of another family
lets that text be retyped with evidence.
"""

from __future__ import annotations

import io
import re
from typing import Any

import cv2
import numpy as np
from PIL import Image

from vwb.gateway import gemini as gem
from vwb.gateway.core import Gateway
from vwb.index.ocr import agree_key

PROMPT = ("Each image below is one line of text cropped from a slide; the line is centred vertically, and slivers "
          "of neighbouring lines at the top or bottom edge must be ignored. Transcribe each exactly as printed: keep "
          "spelling, capitalisation, punctuation, symbols (≥ ≤ ~ % † ‡ * – —) and numbers exactly, and write "
          "superscript reference markers as ^{n} (for example years^{1,2}). Do not correct, expand or complete "
          "anything; if characters are cut off at the edge, write only what is visible. Return JSON "
          '{"lines": [{"image": <number>, "text": "<transcription>"}]} with one entry per image.')
SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {
    "type": "object", "properties": {"image": {"type": "integer"}, "text": {"type": "string"}},
    "required": ["image", "text"]}}}, "required": ["lines"]}


def plain(text: str) -> str:
    return re.sub(r"\^\{([^}]*)\}", r"\1", text or "")


def crop_line(rgb: np.ndarray, box: tuple[float, float, float, float], target_h: int = 48) -> np.ndarray:
    x, y, w, h = box
    mx, my = max(4.0, 0.4 * h), max(2.0, 0.12 * h)     # tight vertically: neighbouring lines sit close
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = int(max(0, x - mx)), int(max(0, y - my)), int(min(W, x + w + mx)), int(min(H, y + h + my))
    crop = rgb[y0:y1, x0:x1]
    k = float(np.clip(target_h / max(h, 1.0), 1.0, 4.0))
    if k > 1.0:
        crop = cv2.resize(crop, (round(crop.shape[1] * k), round(crop.shape[0] * k)), interpolation=cv2.INTER_LANCZOS4)
    return crop


def read(gw: Gateway, crops: list[np.ndarray], batch: int = 10, workers: int = 6) -> list[str]:
    """Transcriptions of the crops, in order. Batches run in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    from google.genai import types
    out: list[str] = [""] * len(crops)

    def one(b0: int) -> None:
        chunk = crops[b0:b0 + batch]
        contents: list[Any] = [PROMPT]
        for i, c in enumerate(chunk, start=1):
            buf = io.BytesIO()
            Image.fromarray(c).save(buf, format="PNG")
            contents += [f"Image {i}:", types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")]
        call = gem.generate(gw, "ocr.reader3", contents, purpose="ocr.reader3", schema=SCHEMA, resolution="high")
        for row in (call.data or {}).get("lines", []):
            i = int(row.get("image", 0)) - 1
            if 0 <= i < len(chunk):
                out[b0 + i] = str(row.get("text", "")).strip()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, range(0, len(crops), batch)))
    return out


def settle(text1: str, text2: str, text3: str) -> tuple[str, bool]:
    """Two of three agreeing (after glyph folding) settles a line; returns (text, agreed)."""
    k1, k2, k3 = agree_key(text1), agree_key(text2 or ""), agree_key(plain(text3))
    if k3 and k3 == k1:
        return text1, True
    if k3 and k3 == k2:
        return text2, True
    return text1, False
