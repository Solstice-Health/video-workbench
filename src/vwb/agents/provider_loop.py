"""Persistent, host-only Anthropic and OpenRouter main-agent tool loops.

Native provider messages (including thinking signatures and OpenRouter reasoning details)
are kept in request-local memory, outside the agent's work directory. Tools always pass
through Registry.call. A stopped call is never blindly executed again on resume.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from vwb.agents.events import MAX_TOOL_CHARS, emit
from vwb.config import secrets
from vwb.tools.base import Registry, ToolContext, ToolResult
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import read_json, write_json

ENDPOINTS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
MAX_TOOL_TEXT = 64000
INTERRUPTED_RESULT = (
    "ERROR: The previous turn stopped before a tool result was recorded. The tool may have taken effect. "
    "Inspect the request state before deciding whether to repeat this action."
)


class ProviderError(RuntimeError):
    """A safe, actionable error with no upstream bodies, headers or credentials."""


def history_path(ctx: ToolContext, provider: str, model: str) -> Path:
    ident = sha256_text(f"{provider}:{model}")[:24]
    return ctx.ws.memory / "providers" / f"{ident}.json"


def _redact(value: Any, key: str) -> Any:
    if isinstance(value, str):
        return value.replace(key, "[redacted]")
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


def _post(provider: str, body: dict[str, Any], on_activity: Callable[[str, str], None] | None = None) -> dict[str, Any]:
    """No automatic retry: a lost response can already have incurred a provider charge."""
    key = secrets().require(provider)
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if body.get("speed") == "fast":
            headers["anthropic-beta"] = "fast-mode-2026-02-01"
    else:
        headers = {"authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=httpx.Timeout(3900.0, connect=30.0)) as client:
            with client.stream("POST", ENDPOINTS[provider], headers=headers, json=body) as response:
                if response.status_code >= 400:
                    raise ProviderError(f"{provider} rejected this call (HTTP {response.status_code}). Check the key and model settings.")
                stream = _Stream(provider, on_activity)
                payload: list[str] = []
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        payload.append(line[5:].lstrip())
                    elif not line and payload:
                        stream.feed(_redact("\n".join(payload), key))
                        payload = []
                if payload:
                    stream.feed(_redact("\n".join(payload), key))
                return _redact(stream.finish(), key)
    except httpx.TimeoutException:
        raise ProviderError(f"{provider} timed out. Your conversation is saved; try again.") from None
    except httpx.HTTPError:
        raise ProviderError(f"Could not reach {provider}. Your conversation is saved; try again.") from None


class _Stream:
    """Assemble native provider messages and publish only public text deltas."""

    def __init__(self, provider: str, activity: Callable[[str, str], None] | None = None):
        self.provider, self.activity = provider, activity
        self.data: dict[str, Any] = {}
        self.blocks: dict[int, dict[str, Any]] = {}
        self.arguments: dict[int, str] = {}
        self.closed: set[int] = set()
        self.calls: dict[int, dict[str, Any]] = {}
        self.details: dict[Any, dict[str, Any]] = {}
        self.answer, self.thought = "", ""
        self.complete = False

    def _publish(self) -> None:
        if self.activity:
            if self.answer:
                self.activity("message", self.answer)
            if self.thought:
                self.activity("thinking", self.thought)

    def feed(self, raw: str) -> None:
        if raw == "[DONE]":
            self.complete = True
            return
        try:
            event = json.loads(raw)
        except ValueError:
            raise ProviderError(f"{self.provider} returned malformed streaming data.") from None
        if not isinstance(event, dict) or event.get("error") or event.get("type") == "error":
            raise ProviderError(f"{self.provider} reported a streaming error.")
        if self.provider == "anthropic":
            self._anthropic(event)
        else:
            self._openrouter(event)
        self._publish()

    def _anthropic(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message_start":
            if self.data:
                raise ProviderError("Anthropic returned multiple messages in one stream.")
            self.data = copy.deepcopy(event.get("message") or {})
        elif kind == "content_block_start":
            index, block = event.get("index"), event.get("content_block")
            if type(index) is not int or not isinstance(block, dict) or index in self.blocks:
                raise ProviderError("Anthropic returned an invalid streaming block.")
            self.blocks[index] = copy.deepcopy(block)
        elif kind == "content_block_delta":
            index, delta = event.get("index"), event.get("delta") or {}
            if index not in self.blocks or index in self.closed or not isinstance(delta, dict):
                raise ProviderError("Anthropic returned an invalid streaming delta.")
            block = self.blocks[index]
            for delta_type, field in (("text_delta", "text"), ("thinking_delta", "thinking"),
                                      ("signature_delta", "signature")):
                if delta.get("type") == delta_type:
                    piece = delta.get(field)
                    if not isinstance(piece, str):
                        raise ProviderError("Anthropic returned a malformed text delta.")
                    block[field] = str(block.get(field) or "") + piece
                    if field == "text":
                        self.answer += piece
                    elif field == "thinking":
                        self.thought += piece
                    break
            else:
                if delta.get("type") == "input_json_delta":
                    fragment = delta.get("partial_json")
                    if not isinstance(fragment, str):
                        raise ProviderError("Anthropic returned malformed tool arguments.")
                    self.arguments[index] = self.arguments.get(index, "") + fragment
                elif delta.get("type") == "citations_delta":
                    block.setdefault("citations", []).append(delta.get("citation"))
                else:
                    raise ProviderError("Anthropic returned an unsupported streaming delta.")
        elif kind == "content_block_stop":
            index = event.get("index")
            if index not in self.blocks:
                raise ProviderError("Anthropic ended an unknown streaming block.")
            if index in self.arguments:
                try:
                    self.blocks[index]["input"] = json.loads(self.arguments[index])
                except ValueError:
                    raise ProviderError("Anthropic returned incomplete tool arguments.") from None
            self.closed.add(index)
        elif kind == "message_delta":
            self.data.update(event.get("delta") or {})
            self.data.setdefault("usage", {}).update(event.get("usage") or {})
        elif kind == "message_stop":
            self.complete = True
        elif kind != "ping":
            raise ProviderError("Anthropic returned an unsupported streaming event.")

    def _openrouter(self, event: dict[str, Any]) -> None:
        for field in ("id", "model", "provider", "service_tier"):
            if field in event:
                self.data[field] = event[field]
        if event.get("usage") is not None:
            self.data["usage"] = event["usage"]
        choices = event.get("choices") or []
        if not isinstance(choices, list) or len(choices) > 1:
            raise ProviderError("OpenRouter returned invalid streaming choices.")
        if not choices:
            return  # final accounting chunks have no choices
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            raise ProviderError("OpenRouter returned an invalid streaming choice.")
        if choice.get("finish_reason") is not None:
            self.data["finish_reason"] = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict) or delta.get("refusal"):
            raise ProviderError("OpenRouter declined the request or returned an invalid streaming delta.")
        if delta.get("content") is not None:
            if not isinstance(delta["content"], str):
                raise ProviderError("OpenRouter returned unsupported streaming content.")
            self.answer += delta["content"]
        if isinstance(delta.get("reasoning"), str):
            self.thought += delta["reasoning"]
        for part in delta.get("reasoning_details") or []:
            if not isinstance(part, dict):
                raise ProviderError("OpenRouter returned malformed reasoning details.")
            ident = part.get("index", part.get("id", len(self.details)))
            old = self.details.setdefault(ident, {})
            for name, value in part.items():
                if name in ("text", "summary", "data") and isinstance(value, str):
                    old[name] = str(old.get(name) or "") + value
                else:
                    old[name] = copy.deepcopy(value)
            if not delta.get("reasoning") and part.get("type") in ("reasoning.text", "reasoning.summary"):
                self.thought += str(part.get("text") or part.get("summary") or "")
        for part in delta.get("tool_calls") or []:
            if not isinstance(part, dict) or type(part.get("index")) is not int:
                raise ProviderError("OpenRouter returned a malformed streaming tool call.")
            call = self.calls.setdefault(part["index"], {"type": "function", "function": {"name": "", "arguments": ""}})
            for field in ("id", "type"):
                if field in part:
                    call[field] = part[field]
            fn = part.get("function") or {}
            if not isinstance(fn, dict):
                raise ProviderError("OpenRouter returned an invalid streaming function.")
            for field in ("name", "arguments"):
                if field in fn:
                    if not isinstance(fn[field], str):
                        raise ProviderError("OpenRouter returned invalid streaming function fields.")
                    call["function"][field] += fn[field]

    def finish(self) -> dict[str, Any]:
        if not self.complete:
            raise ProviderError(f"{self.provider} stream ended before completion. Your conversation is saved.")
        if self.provider == "anthropic":
            if not self.data or set(self.blocks) != self.closed:
                raise ProviderError("Anthropic stream ended with incomplete message blocks.")
            self.data["content"] = [self.blocks[i] for i in sorted(self.blocks)]
            return self.data
        message: dict[str, Any] = {"role": "assistant", "content": self.answer or None}
        if self.thought:
            message["reasoning"] = self.thought
        if self.details:
            message["reasoning_details"] = list(self.details.values())
        if self.calls:
            message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return {**self.data, "choices": [{"message": message, "finish_reason": self.data.get("finish_reason")} ]}


def _image_dimensions(encoded: str) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as img:
            width, height = img.size
    except (ValueError, OSError):
        raise ProviderError("A saved tool image is invalid. No model call was sent.") from None
    if width <= 0 or height <= 0:
        raise ProviderError("A saved tool image has invalid dimensions. No model call was sent.")
    return width, height


def _image_tokens(dimensions: tuple[int, int] | None, provider: str | None) -> int:
    """Estimate visual tokens, independently from the base64 transport length.

    Anthropic documents 28x28-pixel visual patches. Counting the original dimensions
    plus overhead is conservative because any provider downscale reduces the patches.
    OpenRouter has many encoders: two tokens per 14x14-pixel patch plus overhead is a
    deliberately roomy heuristic for our fitted frames, not a tokenizer guarantee.
    Remote images without readable dimensions use a fixed 16K-token budget estimate.
    """
    if dimensions is None:
        return 16384
    width, height = dimensions
    if provider == "anthropic":
        return math.ceil(width / 28) * math.ceil(height / 28) + 512
    return max(4096, 2 * math.ceil(width / 14) * math.ceil(height / 14) + 1024)


def estimate_tokens(value: Any, *, provider: str | None = None) -> int:
    """History budget estimate, not an exact tokenizer measurement.

    Text/JSON retains the conservative UTF-8-byte estimate. Inline image transport is
    replaced with a small dimension marker and visual tokens estimated separately.
    Anthropic image sizing follows its published patch formula; OpenRouter is a
    heuristic because generic models do not share an image tokenizer. Actual usage
    from the completed provider call remains the only measured context count.
    """
    image_tokens = 0

    def without_image_payload(node: Any) -> Any:
        nonlocal image_tokens
        if isinstance(node, dict):
            if node.get("type") == "image" and isinstance(node.get("source"), dict):
                source = node["source"]
                dimensions = (_image_dimensions(source["data"])
                              if source.get("type") == "base64" and isinstance(source.get("data"), str) else None)
                image_tokens += _image_tokens(dimensions, provider)
                return {"type": "image", "dimensions": dimensions}
            if node.get("type") == "image_url" and isinstance(node.get("image_url"), dict):
                url = node["image_url"].get("url", "")
                dimensions = (_image_dimensions(url.split(",", 1)[1])
                              if isinstance(url, str) and url.startswith("data:image/") and ";base64," in url else None)
                image_tokens += _image_tokens(dimensions, provider)
                return {"type": "image_url", "dimensions": dimensions,
                        "detail": node["image_url"].get("detail")}
            return {key: without_image_payload(item) for key, item in node.items()}
        if isinstance(node, list):
            return [without_image_payload(item) for item in node]
        return node

    text_bytes = len(json.dumps(without_image_payload(value), ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"))
    return text_bytes + image_tokens + 256


def _check_context(body: dict[str, Any], cfg: dict[str, Any]) -> int:
    limit = int(cfg.get("context_window", 300000))
    output = int(cfg.get("max_output_tokens", 16384))
    estimated = estimate_tokens(body, provider=cfg.get("provider"))
    if estimated + output > limit:
        raise ProviderError(
            f"This conversation exceeds the selected {limit:,}-token context budget under the "
            "history estimate. Choose a larger context or start a new conversation. No model call was sent."
        )
    return estimated


def _tools(registry: Registry, role: str, provider: str) -> list[dict[str, Any]]:
    if provider == "anthropic":
        return [{"name": t.name, "description": t.description, "input_schema": t.schema}
                for t in registry.for_role(role)]
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                               "parameters": t.schema}}
            for t in registry.for_role(role)]


def _request(provider: str, cfg: dict[str, Any], instructions: str, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]]) -> dict[str, Any]:
    maximum = int(cfg.get("max_output_tokens", 16384))
    if provider == "anthropic":
        body: dict[str, Any] = {"model": cfg["model"], "system": instructions, "messages": messages,
                                "max_tokens": maximum, "stream": True}
        if cfg.get("effort"):
            body.update(thinking={"type": "adaptive", "display": "summarized"}, output_config={"effort": cfg["effort"]})
        if cfg.get("speed") == "fast":
            body["speed"] = "fast"
    else:
        body = {"model": cfg["model"], "messages": [{"role": "system", "content": instructions}, *messages],
                "max_tokens": maximum, "stream": True, "stream_options": {"include_usage": True},
                "provider": {"require_parameters": True}}
        if cfg.get("effort"):
            # The catalog validates the exact model's reported reasoning capability.
            body["reasoning"] = {"effort": cfg["effort"]}
        if cfg.get("service_tier") in ("fast", "ultrafast"):
            body["service_tier"] = cfg["service_tier"]
    if tools:
        body["tools"] = tools
    return body


def _usage(provider: str, data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("usage") or {}
    if not isinstance(raw, dict) or not raw:
        return {}
    result = copy.deepcopy(raw)
    if provider == "openrouter":
        for target, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                               ("input_tokens_details", "prompt_tokens_details"),
                               ("output_tokens_details", "completion_tokens_details")):
            if source in raw:
                result[target] = raw[source]
    elif type(raw.get("input_tokens")) is int:
        cached = raw.get("cache_read_input_tokens") or 0
        created = raw.get("cache_creation_input_tokens") or 0
        if type(cached) is not int or type(created) is not int:
            return {}
        result["input_tokens"] = raw["input_tokens"] + cached + created
        result["input_tokens_details"] = {"cached_tokens": cached, "cache_creation_tokens": created}
    if any(type(result.get(field)) is not int or result[field] < 0 for field in ("input_tokens", "output_tokens")):
        return {}  # missing/invalid measurements are unavailable, never zero guessed from absence
    result.setdefault("total_tokens", result["input_tokens"] + result["output_tokens"])
    return result


def _parse_anthropic(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    if data.get("type") == "error" or data.get("error"):
        raise ProviderError("Anthropic reported a model error. Your conversation is saved.")
    reason = data.get("stop_reason")
    if reason not in ("end_turn", "tool_use"):
        raise ProviderError(f"Anthropic did not finish the turn ({reason or 'missing stop reason'}). Your conversation is saved.")
    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise ProviderError("Anthropic returned an empty or malformed message.")
    calls, texts, thoughts = [], [], []
    for block in content:
        if not isinstance(block, dict):
            raise ProviderError("Anthropic returned a malformed message block.")
        typ = block.get("type")
        if typ == "tool_use":
            if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str) or not isinstance(block.get("input"), dict):
                raise ProviderError("Anthropic returned a malformed tool call. No tools were executed.")
            calls.append({"id": block["id"], "name": block["name"], "arguments": block["input"]})
        elif typ == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif typ == "thinking" and isinstance(block.get("thinking"), str):
            thoughts.append(block["thinking"])
        elif typ == "redacted_thinking":
            pass  # replay provider-native redacted blocks; never expose them as public thinking
        else:
            raise ProviderError("Anthropic returned an unsupported message block.")
    if len({c["id"] for c in calls}) != len(calls) or bool(calls) != (reason == "tool_use"):
        raise ProviderError("Anthropic returned inconsistent tool calls. No tools were executed.")
    text = "\n".join(texts)
    if not calls and not text.strip():
        raise ProviderError("Anthropic returned no final answer.")
    return {"role": "assistant", "content": copy.deepcopy(content)}, calls, text, "\n".join(thoughts)


def _parse_openrouter(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    if data.get("error"):
        raise ProviderError("OpenRouter reported a model error. Your conversation is saved.")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderError("OpenRouter returned no valid model choice.")
    choice = choices[0]
    msg = choice.get("message")
    if not isinstance(msg, dict) or msg.get("refusal") or choice.get("finish_reason") == "content_filter":
        raise ProviderError("OpenRouter declined the request or returned an invalid message.")
    reason = choice.get("finish_reason")
    if reason not in ("stop", "tool_calls"):
        raise ProviderError(f"OpenRouter did not finish the turn ({reason or 'missing finish reason'}). Your conversation is saved.")
    content = msg.get("content")
    if isinstance(content, list):
        if any(not isinstance(b, dict) or b.get("type") not in ("text", "output_text") for b in content):
            raise ProviderError("OpenRouter returned an unsupported or refused message block.")
        text = "\n".join(str(b.get("text") or "") for b in content)
    elif content is None or isinstance(content, str):
        text = content or ""
    else:
        raise ProviderError("OpenRouter returned malformed message content.")
    raw_calls = msg.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise ProviderError("OpenRouter returned malformed tool calls.")
    calls = []
    for call in raw_calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if (not isinstance(fn, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str)
                or not isinstance(fn.get("name"), str) or not isinstance(fn.get("arguments"), str)):
            raise ProviderError("OpenRouter returned a malformed tool call. No tools were executed.")
        try:
            args = json.loads(fn["arguments"])
        except ValueError:
            raise ProviderError("OpenRouter returned invalid tool arguments. No tools were executed.") from None
        if not isinstance(args, dict):
            raise ProviderError("OpenRouter returned non-object tool arguments. No tools were executed.")
        calls.append({"id": call["id"], "name": fn["name"], "arguments": args})
    if len({c["id"] for c in calls}) != len(calls) or bool(calls) != (reason == "tool_calls"):
        raise ProviderError("OpenRouter returned inconsistent tool calls. No tools were executed.")
    if not calls and not text.strip():
        raise ProviderError("OpenRouter returned no final answer.")
    # Keep only replay fields, preserving all native reasoning_details including encrypted entries.
    message = {"role": "assistant", "content": content}
    for field in ("tool_calls", "reasoning_details", "reasoning"):
        if field in msg:
            message[field] = copy.deepcopy(msg[field])
    thought = msg.get("reasoning") if isinstance(msg.get("reasoning"), str) else ""
    details = msg.get("reasoning_details")
    if isinstance(details, list):
        public = [d.get("text") or d.get("summary") for d in details
                  if isinstance(d, dict) and d.get("type") in ("reasoning.text", "reasoning.summary")]
        thought = thought or "\n".join(str(v) for v in public if v)
    return message, calls, text, thought


def _tool_result(provider: str, call: dict[str, Any], result: ToolResult) -> list[dict[str, Any]]:
    text = result.text[:MAX_TOOL_TEXT] or "(no text)"
    if provider == "anthropic":
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for img in result.images[:6]:
            mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                          "data": base64.b64encode(img).decode()}})
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                                "content": blocks, "is_error": result.is_error}]}]
    rows = [{"role": "tool", "tool_call_id": call["id"], "content": text}]
    if result.images:
        parts = [{"type": "text", "text": f"Images returned by {call['name']} ({call['id']})."}]
        for img in result.images[:6]:
            mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(img).decode()}"}})
        # Images are appended after all tool-call results, preserving tool message adjacency.
        rows.append({"role": "user", "content": parts})
    return rows


def _recover_pending(state: dict[str, Any], provider: str) -> None:
    """Close saved incomplete tool exchanges without repeating potentially side-effectful tools."""
    pending = state.get("pending") or {}
    done = set(pending.get("completed", []))
    for call in pending.get("calls", []):
        if call["id"] not in done:
            state["messages"].extend(_tool_result(provider, call, ToolResult.error(INTERRUPTED_RESULT)))
    state["messages"].extend(pending.get("images", []))
    state.pop("pending", None)


def _save_context(ctx: ToolContext, cfg: dict[str, Any], usage: dict[str, Any], estimated: int, key: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    used = usage.get("total_tokens") if "input_tokens" in usage else None
    row = {"key": key, "provider": cfg["provider"], "model": cfg["model"], "used_tokens": used,
           "limit_tokens": int(cfg.get("context_window", 300000)),
           "native_context_window": cfg.get("native_context_window"), "estimated_tokens": estimated,
           "measured_at": now, "breakdown": usage, "source": "provider_usage", "compact_available": False}
    write_json(ctx.ws.memory / "provider_context.json", row)
    emit(ctx.ws, "usage", {"source": cfg["provider"], "scope": "call", "usage": usage,
                            "context_window": row["limit_tokens"], "estimated_tokens": estimated})


def run(ctx: ToolContext, registry: Registry, cfg: dict[str, Any], instructions: str, text: str, *,
        turn_id: str, initial_messages: list[dict[str, Any]] | None = None) -> str:
    """Run one persistent main-agent turn. Caller holds the workspace turn lock."""
    provider, model = cfg["provider"], cfg["model"]
    if provider not in ENDPOINTS:
        raise ProviderError(f"No direct main-agent host for {provider}.")
    path = history_path(ctx, provider, model)
    state = read_json(path, {}) or {}
    if not state:
        state = {"version": 1, "provider": provider, "model": model,
                 "messages": copy.deepcopy(initial_messages or [])}
    if not isinstance(state.get("messages"), list) or state.get("provider") != provider or state.get("model") != model:
        raise ProviderError("The saved provider conversation is invalid. Start a new conversation.")
    _recover_pending(state, provider)
    if state.get("turn_id") != turn_id:
        state["messages"].append({"role": "user", "content": text})
        state["turn_id"] = turn_id
    write_json(path, state)
    tools = _tools(registry, ctx.role, provider)
    for step in range(int(cfg.get("max_turns", 400))):
        body = _request(provider, cfg, instructions, state["messages"], tools)
        estimated = _check_context(body, cfg)
        started = time.time()
        ident = f"{provider}:{turn_id}:{step}"
        emit(ctx.ws, "thinking", {"id": ident, "source": provider, "status": "in_progress",
                                  "text": "Considering your request."})
        usage: dict[str, Any] = {}
        streamed: dict[str, str] = {}
        try:
            def activity(kind: str, value: str, activity_base: str = ident,
                         snapshots: dict[str, str] = streamed) -> None:
                snapshots[kind] = value
                activity_id = f"{activity_base}:message" if kind == "message" else activity_base
                emit(ctx.ws, kind, {"id": activity_id, "source": provider, "status": "in_progress", "text": value})

            data = _post(provider, body, activity)
            usage = _usage(provider, data)
            usage.update(service_tier_requested=cfg.get("service_tier"), service_tier_served=data.get("service_tier"),
                         speed_requested=cfg.get("speed"), speed_served=data.get("speed"))
            assistant, calls, answer, thinking = (_parse_anthropic(data) if provider == "anthropic"
                                                  else _parse_openrouter(data))
        except BaseException as exc:
            label = "stopped" if isinstance(exc, KeyboardInterrupt) else "failed"
            emit(ctx.ws, "thinking", {"id": ident, "source": provider, "status": label,
                                      "text": "Stopped." if label == "stopped" else "The model call failed."})
            if streamed.get("message"):
                emit(ctx.ws, "message", {"id": f"{ident}:message", "source": provider, "status": label,
                                         "text": streamed["message"]})
            if isinstance(exc, Exception):
                ctx.gateway.record(slot_name=ctx.role, provider=provider, model=model, purpose=f"agent:{ctx.agent}",
                                   request=body, response_text="", usage=usage, seconds=time.time() - started,
                                   ok=False, error=str(exc) if isinstance(exc, ProviderError) else "Provider call failed")
            raise
        state["messages"].append(assistant)
        state["usage"] = usage
        if calls:
            state["pending"] = {"calls": calls, "completed": [], "images": []}
        write_json(path, state)
        ctx.gateway.record(slot_name=ctx.role, provider=provider, model=model, purpose=f"agent:{ctx.agent}",
                           request=body, response_text=answer, usage=usage, seconds=time.time() - started, ok=True)
        _save_context(ctx, cfg, usage, estimated, f"{cfg['host']}:{model}")
        emit(ctx.ws, "thinking", {"id": ident, "source": provider, "status": "completed",
                                  "text": thinking or "Finished considering your request."})
        if answer:
            emit(ctx.ws, "message", {"id": f"{ident}:message", "source": provider,
                                     "status": "completed", "text": answer})
        if not calls:
            state["finished_turn"] = turn_id
            write_json(path, state)
            return answer
        for call in calls:
            detail = {"id": f"{provider}:{call['id']}", "source": provider, "name": call["name"],
                      "arguments": call["arguments"]}
            state["pending"]["executing"] = call["id"]
            write_json(path, state)
            emit(ctx.ws, "tool", {**detail, "status": "in_progress"})
            try:
                result = registry.call(ctx, call["name"], call["arguments"])
            except BaseException:
                emit(ctx.ws, "tool", {**detail, "status": "stopped", "output": "Stopped before a result was saved."})
                raise
            rows = _tool_result(provider, call, result)
            state["messages"].append(rows[0])
            if len(rows) > 1:
                state["pending"]["images"].extend(rows[1:])
            state["pending"]["completed"].append(call["id"])
            state["pending"].pop("executing", None)
            write_json(path, state)
            emit(ctx.ws, "tool", {**detail, "status": "failed" if result.is_error else "completed",
                                  "output": result.text[:MAX_TOOL_CHARS]})
        state["messages"].extend(state["pending"]["images"])
        state.pop("pending", None)
        write_json(path, state)
    raise ProviderError("The model reached its step budget without a final answer. Your conversation and tool results are saved.")
