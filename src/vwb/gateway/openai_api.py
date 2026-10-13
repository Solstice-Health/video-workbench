"""OpenAI Responses API through the gateway (gpt-6.1-sol at max reasoning effort)."""

from __future__ import annotations

import base64
import time
from typing import Any

from vwb.gateway.core import Gateway, SlotCall, parse_json_loose


def image_input(png_or_jpeg: bytes, mime: str = "image/png", detail: str = "high") -> dict[str, Any]:
    b64 = base64.b64encode(png_or_jpeg).decode()
    return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}", "detail": detail}


def text_input(text: str) -> dict[str, Any]:
    return {"type": "input_text", "text": text}


def user_message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"role": "user", "content": list(parts)}


def replay_items(output: list[Any]) -> list[dict[str, Any]]:
    """A response's output items as input items for the next turn, without server-side state.

    The organisation runs Zero Data Retention, so nothing is stored or chained by id
    (previous_response_id is refused). Reasoning items travel as encrypted content (requested
    with include=["reasoning.encrypted_content"]); calls and messages travel without their ids.
    """
    items: list[dict[str, Any]] = []
    for it in output:
        d = it.model_dump(exclude_none=True) if hasattr(it, "model_dump") else dict(it)
        kind = d.get("type")
        if kind == "reasoning" and d.get("encrypted_content"):
            items.append({"type": "reasoning", "id": d["id"], "summary": d.get("summary", []),
                          "encrypted_content": d["encrypted_content"]})
        elif kind == "function_call":
            items.append({"type": "function_call", "call_id": d["call_id"], "name": d["name"],
                          "arguments": d.get("arguments", "{}")})
        elif kind == "message":
            text = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "output_text")
            if text:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
    return items


def respond(gw: Gateway, slot_name: str, input_items: list[dict[str, Any]], *, purpose: str,
            instructions: str | None = None, schema: dict | None = None, schema_name: str = "result",
            tools: list[dict] | None = None) -> SlotCall:
    """One Responses API call with retries and fallbacks. Returns text, parsed JSON, and the raw response."""

    def call(cfg: dict[str, Any]) -> SlotCall:
        model = cfg["model"]
        kwargs: dict[str, Any] = {"model": model, "input": input_items, "store": False}
        if cfg.get("effort"):
            kwargs["reasoning"] = {"effort": cfg["effort"]}
        if instructions:
            kwargs["instructions"] = instructions
        if schema is not None:
            kwargs["text"] = {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": False}}
        if tools:
            kwargs["tools"] = tools
        t0 = time.time()
        request_sig = {"model": model, "purpose": purpose, "instructions": (instructions or "")[:4000],
                       "n_items": len(input_items), "schema": schema_name if schema else None}
        try:
            resp = gw.openai().responses.create(**kwargs)
            text = resp.output_text or ""
            usage = resp.usage.model_dump() if resp.usage else {}
            data = parse_json_loose(text) if (schema is not None and not tools) else None
        except Exception as exc:
            gw.record(slot_name=slot_name, provider="openai", model=model, purpose=purpose, request=request_sig,
                      response_text="", usage=None, seconds=time.time() - t0, ok=False, error=str(exc))
            raise
        gw.record(slot_name=slot_name, provider="openai", model=model, purpose=purpose, request=request_sig,
                  response_text=text, usage=usage, seconds=time.time() - t0, ok=True)
        return SlotCall(slot=slot_name, provider="openai", model=model, text=text, data=data, usage=usage, raw=resp)

    return gw.with_fallbacks(slot_name, call)
