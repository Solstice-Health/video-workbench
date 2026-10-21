"""Attachment identities on disk; provider-specific pixels only at the send boundary."""
from __future__ import annotations

from typing import Any

from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace, attachments

IMAGE_REF = "vwb_attachment_image"
MAX_INLINE_BYTES = 16 * 1024**2  # ~22 MiB after base64; leave room for text/tools
MAX_INLINE_IMAGES = 24


def latest(ws: Workspace) -> list[dict[str, Any]]:
    chat = ws.load().chat
    return chat[-1].attachments if chat and chat[-1].role == "human" else []


def unique(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({ref["id"]: ref for ref in refs}.values())


def catalog(ws: Workspace) -> list[dict[str, Any]]:
    if not ws.request_path.is_file():
        return []
    return unique([ref for turn in ws.load().chat for ref in turn.attachments])


def catalog_note(ws: Workspace) -> str:
    refs = catalog(ws)
    if not refs:
        return ""
    return (f"\n[harness] {len(refs)} attached file(s) remain available in this conversation. "
            "Use attachment_list for their identities and paths, and attachment_read to inspect a pinned original. "
            "Compaction does not remove originals or their working copies.")


def snapshot_catalog(ws: Workspace) -> None:
    # An independent compaction checkpoint; working files cannot overwrite it.
    # The tool-visible catalog is always reconstructed from public chat.
    write_json(ws.memory / "attachments.json", {"files": catalog(ws)})


def content(text: str, refs: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    images = [ref for ref in unique(refs) if ref.get("kind") == "image"]
    if not images:
        return text
    return [{"type": "text", "text": text}, *[{"type": IMAGE_REF, "ref": ref} for ref in images]]


def hydrate(ws: Workspace, value: Any, provider: str, _budget: dict[str, int] | None = None, *,
            allow_images: bool = True) -> Any:
    """Expand internal image references, without mutating persisted provider history."""
    budget = _budget if _budget is not None else {"bytes": 0, "images": 0}
    if isinstance(value, list):
        # Newest images take priority, without changing message/block order.
        return list(reversed([hydrate(ws, item, provider, budget, allow_images=allow_images) for item in reversed(value)]))
    if not isinstance(value, dict):
        return value
    if "role" in value:
        # Never interpret assistant/tool argument JSON or signed reasoning as
        # harness-owned references. Only user content carries these markers.
        if value.get("role") == "user" and isinstance(value.get("content"), list):
            return {**value, "content": hydrate(ws, value["content"], provider, budget, allow_images=allow_images)}
        return value
    if value.get("type") == IMAGE_REF:
        try:
            ref = attachments.canonical(ws, value["ref"])
            note = (read_json(ws.memory / "attachment_notes.json", {}) or {}).get(ref["id"], {})
            if not allow_images:
                return {"type": "text", "text": f"[harness] Image {ref['id']} at {ref['path']} retained as a file reference. "
                        "This model has no verified image-input support; no pixels were sent. "
                        f"Saved observation: {note.get('summary', '')}. Use text/OCR tools or a model with image input for visual examination."}
            if note.get("image_mode") == "reference":
                return {"type": "text", "text": f"[harness] Retained image reference {ref['id']} at {ref['path']}. "
                        f"Saved observation: {note.get('summary', '')}. Use attachment_read to inspect pixels again."}
            payload = attachments.image_payload(ws, ref)
        except (OSError, ValueError) as exc:
            # Missing pixels must be visible to the model; other files/text can
            # still be processed and the durable reference stays in history.
            return {"type": "text", "text": f"[harness] Image payload unavailable: {exc}. Use its attachment path or ask for the file again."}
        if payload is None:
            return {"type": "text", "text": "[harness] This attachment is not a supported raster image; inspect it with tools."}
        size = len(payload["base64"]) * 3 // 4
        if budget["bytes"] + size > MAX_INLINE_BYTES or budget["images"] >= MAX_INLINE_IMAGES:
            return {"type": "text", "text": f"[harness] Image {ref['id']} at {ref['path']} retained as a reference "
                    "to keep this request within its image payload budget. Use attachment_read to examine it."}
        budget["bytes"] += size
        budget["images"] += 1
        if provider == "anthropic":
            return {"type": "image", "source": {"type": "base64", "media_type": payload["mime"], "data": payload["base64"]}}
        url = f"data:{payload['mime']};base64,{payload['base64']}"
        if provider == "openai":
            return {"type": "input_image", "image_url": url}
        if provider == "openrouter":
            return {"type": "image_url", "image_url": {"url": url}}
        raise ValueError(f"Unsupported attachment image provider: {provider}")
    return value


def input_parts(ws: Workspace, text: str, refs: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    raw = content(text, refs)
    parts = hydrate(ws, raw, provider) if isinstance(raw, list) else [{"type": "text", "text": raw}]
    if provider == "openai":
        for part in parts:
            if part.get("type") == "text":
                part["type"] = "input_text"
    return parts
