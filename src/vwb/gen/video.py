"""The Omni video model (slot vid.omni) through the Interactions API.

Tasks and the inputs they take:
  text       a prompt only
  frames     a first frame, and optionally a last frame: the model animates from one to the other
  reference  1 to 3 reference images the prompt cites as <IMAGE_REF_0>, <IMAGE_REF_1>, ...
  edit       a clip (up to 10 s) and an instruction
  extend     a clip (up to 10 s) to continue
Clips are 3 to 10 s, 24 fps, at 360p, 720p or 1080p, 9:16 or 16:9, with a generated audio
track (the harness does not use it). The model has no alpha: for an element over a design,
the caller gives it a key-colour plate and keys the result (gen.key).
"""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from vwb.config import slot
from vwb.gateway import gemini as gem
from vwb.gateway.core import Gateway

SLOT = "vid.omni"
TASKS = {"text": "text_to_video", "frames": "image_to_video", "reference": "reference_to_video", "edit": "edit",
         "extend": "extend"}


def _image(rgb: np.ndarray) -> dict[str, Any]:
    buf = io.BytesIO()
    Image.fromarray(rgb[..., :3]).save(buf, format="PNG")
    return {"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "mime_type": "image/png"}


def generate(gw: Gateway, *, task: str, prompt: str, out: Path, first: np.ndarray | None = None,
             last: np.ndarray | None = None, refs: list[np.ndarray] | None = None, video: Path | None = None,
             seconds: int = 6, resolution: str | None = None, aspect: str = "9:16", seed: int | None = None,
             purpose: str = "omni") -> dict[str, Any]:
    if task not in TASKS:
        raise ValueError(f"task must be one of {sorted(TASKS)}")
    seconds = int(min(10, max(3, seconds)))
    parts: list[dict[str, Any]] = []
    if task == "frames":
        if first is None:
            raise ValueError("task 'frames' needs a first frame")
        parts += [_image(first)] + ([_image(last)] if last is not None else [])
    elif task == "reference":
        if not refs:
            raise ValueError("task 'reference' needs 1 to 3 reference images")
        parts += [_image(r) for r in refs[:3]]
    elif task in ("edit", "extend"):
        if video is None:
            raise ValueError(f"task {task!r} needs a clip")
        f = gem.upload(gw, video)
        parts.append({"type": "video", "uri": f.uri})
    parts.append({"type": "text", "text": prompt})
    cfg = slot(SLOT)
    res = resolution or cfg.get("resolution", "1080p")
    fmt = {"type": "video", "aspect_ratio": aspect, "resolution": res, "duration": f"{seconds}s"}
    gen_cfg: dict[str, Any] = {"video_config": {"task": TASKS[task]}}
    if seed is not None:
        gen_cfg["seed"] = int(seed)
    request = {"task": task, "prompt": prompt, "format": fmt, "n_images": sum(p["type"] == "image" for p in parts)}
    t0 = time.time()
    client = gw.google()
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            # a clip takes 25-60 s; a request that hangs is cut and retried rather than stalling the job
            it = client.interactions.create(model=cfg["model"], input=parts if len(parts) > 1 else prompt,
                                            response_format=fmt, generation_config=gen_cfg,
                                            timeout=float(cfg.get("timeout_s", 300)))
            break
        except Exception as exc:  # noqa: BLE001 - retried below when transient
            last_err = exc
            if not any(s in str(exc) for s in ("429", "500", "502", "503", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
                                                "timeout", "Timeout", "timed out")):
                gw.record(slot_name=SLOT, provider="google", model=cfg["model"], purpose=purpose, request=request,
                          response_text="", usage=None, seconds=time.time() - t0, ok=False, error=str(exc))
                raise
            time.sleep(min(60, 8 * 2 ** attempt))
    else:
        raise RuntimeError(f"Omni failed after retries: {last_err}")
    ov = it.output_video
    out.parent.mkdir(parents=True, exist_ok=True)
    if getattr(ov, "data", None):
        out.write_bytes(base64.b64decode(ov.data))
    elif getattr(ov, "uri", None):
        name = ov.uri.split("/")[-1]
        for _ in range(120):
            if client.files.get(name=f"files/{name}").state.name == "ACTIVE":
                break
            time.sleep(3)
        client.files.download(file=ov.uri, destination=str(out))
    else:
        raise RuntimeError("Omni returned no video")
    usage = it.usage.model_dump(exclude_none=True) if getattr(it, "usage", None) else {}
    gw.record(slot_name=SLOT, provider="google", model=cfg["model"], purpose=purpose, request=request,
              response_text=f"video {out.name}", usage=usage, seconds=time.time() - t0, ok=True)
    return {"path": out, "model": cfg["model"], "seconds": round(time.time() - t0, 1), "format": fmt, "task": TASKS[task]}
