"""Gemini through the gateway: a Files API cache, video parts and structured calls.

Provider file handles expire (Gemini Files API uploads last 48 hours, spec R14), so the
cache records the expiry and re-uploads from our durable original when needed.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.genai import types

from vwb.gateway.core import Gateway, SlotCall, parse_json_loose
from vwb.util.hashing import sha256_file
from vwb.util.jsonio import read_json, write_json
from vwb.util.log import log

_MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4a": "audio/mp4", ".wav": "audio/wav",
         ".mp3": "audio/mpeg", ".png": "image/png", ".jpg": "image/jpeg"}

_RESOLUTION = {"low": "MEDIA_RESOLUTION_LOW", "medium": "MEDIA_RESOLUTION_MEDIUM", "high": "MEDIA_RESOLUTION_HIGH"}


def _cache_path(gw: Gateway) -> Path:
    from vwb.config import RUNS_DIR
    return (gw.ws.index / "gemini_files.json") if gw.ws else (RUNS_DIR / "_gemini_files.json")


def upload(gw: Gateway, path: Path | str) -> types.File:
    """Upload once per content hash; reuse until 47 hours have passed."""
    path = Path(path)
    digest = sha256_file(path)
    cache_file = _cache_path(gw)
    cache = read_json(cache_file, {}) or {}
    hit = cache.get(digest)
    client = gw.google()
    if hit and datetime.fromisoformat(hit["expires"]) > datetime.now(timezone.utc) + timedelta(minutes=30):
        try:
            f = client.files.get(name=hit["name"])
            if f.state and f.state.name == "ACTIVE":
                return f
        except Exception:  # noqa: BLE001 - expired or deleted; re-upload
            pass
    t0 = time.time()
    f = client.files.upload(file=str(path), config={"mime_type": _MIME.get(path.suffix.lower(), "application/octet-stream")})
    while f.state and f.state.name == "PROCESSING":
        time.sleep(2)
        f = client.files.get(name=f.name)
    if not f.state or f.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini upload failed for {path.name}: {f.state}")
    cache[digest] = {"name": f.name, "uri": f.uri, "mime": f.mime_type,
                     "expires": (datetime.now(timezone.utc) + timedelta(hours=47)).isoformat()}
    write_json(cache_file, cache)
    gw.calls.record(actor=gw.actor, slot="files", provider="google", model="files.upload", purpose="upload",
                    input_sha256=digest, output_sha256=None, usage={"bytes": path.stat().st_size},
                    seconds=round(time.time() - t0, 2), ok=True, error="")
    log.info("uploaded %s to Gemini Files API in %.1fs", path.name, time.time() - t0)
    return f


def video_part(f: types.File, start: float | None = None, end: float | None = None, fps: float | None = None) -> types.Part:
    meta = None
    if start is not None or end is not None or fps is not None:
        meta = types.VideoMetadata(
            start_offset=f"{start:.3f}s" if start is not None else None,
            end_offset=f"{end:.3f}s" if end is not None else None,
            fps=fps,
        )
    return types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=f.mime_type), video_metadata=meta)


def image_part(png_or_jpeg: bytes, mime: str = "image/png") -> types.Part:
    return types.Part.from_bytes(data=png_or_jpeg, mime_type=mime)


def gen_config(cfg: dict[str, Any], *, schema: dict | None = None, system: str | None = None,
               tools: list | None = None, resolution: str | None = None) -> types.GenerateContentConfig:
    kwargs: dict[str, Any] = {}
    if system:
        kwargs["system_instruction"] = system
    if schema is not None:
        kwargs["response_mime_type"] = "application/json"
        kwargs["response_json_schema"] = schema
    res = resolution or cfg.get("media_resolution")
    if res:
        kwargs["media_resolution"] = _RESOLUTION.get(res, res)
    level = cfg.get("thinking_level")
    if level:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level.upper())
    if tools:
        kwargs["tools"] = tools
        kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
    return types.GenerateContentConfig(**kwargs)


def generate(gw: Gateway, slot_name: str, contents: list, *, purpose: str, schema: dict | None = None,
             system: str | None = None, resolution: str | None = None) -> SlotCall:
    """One structured or free-text call to a Gemini slot, with retries and fallbacks."""

    def call(cfg: dict[str, Any]) -> SlotCall:
        model = cfg["model"]
        t0 = time.time()
        request_sig = {"model": model, "purpose": purpose, "schema": schema,
                       "contents": [str(c)[:2000] for c in contents]}
        try:
            resp = gw.google().models.generate_content(
                model=model, contents=contents, config=gen_config(cfg, schema=schema, system=system, resolution=resolution))
            text = resp.text or ""
            usage = resp.usage_metadata.model_dump(exclude_none=True) if resp.usage_metadata else {}
            data = parse_json_loose(text) if schema is not None else None
        except Exception as exc:
            gw.record(slot_name=slot_name, provider="google", model=model, purpose=purpose, request=request_sig,
                      response_text="", usage=None, seconds=time.time() - t0, ok=False, error=str(exc))
            raise
        gw.record(slot_name=slot_name, provider="google", model=model, purpose=purpose, request=request_sig,
                  response_text=text, usage=usage, seconds=time.time() - t0, ok=True)
        return SlotCall(slot=slot_name, provider="google", model=model, text=text, data=data, usage=usage, raw=resp)

    return gw.with_fallbacks(slot_name, call)
