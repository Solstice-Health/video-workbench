"""The harness's own function-calling loops: OpenAI Responses API and Gemini.

Agents in these loops have no shell and no file access: they can only emit tool calls,
which the control plane executes through the tool gateway under the policy dial. Vendor
keys stay in this process (invariant I3). Images returned by tools go back to the model as
image inputs, so a worker that cannot watch video can still look at frames.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from vwb.agents.events import MAX_TOOL_CHARS, emit
from vwb.gateway import gemini as gem
from vwb.gateway import openai_api as oai
from vwb.gateway.core import Gateway
from vwb.tools.base import Registry, Tool, ToolContext
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import append_jsonl
from vwb.util.log import log

MAX_TOOL_TEXT = 64000      # the gateway already spills longer results to work/.results/


def _log(ctx: ToolContext, **row: Any) -> None:
    append_jsonl(ctx.ws.swarm / f"{ctx.agent}.transcript.jsonl", {"ts": time.time(), **row})
    if ctx.swarm is not None and ctx.role != "job_lead" and row.get("final"):
        ctx.swarm.progress(ctx.agent, row["final"])


def openai_loop(gw: Gateway, cfg: dict[str, Any], instructions: str, user_parts: list[dict[str, Any]],
                tools: list[Tool], registry: Registry, ctx: ToolContext, max_steps: int = 60,
                stop: threading.Event | None = None, history: list[dict[str, Any]] | None = None) -> str:
    """Stateless Responses API loop: the client holds the whole conversation (see oai.replay_items)."""
    client = gw.openai()
    defs = [{"type": "function", "name": t.name, "description": t.description, "parameters": t.schema, "strict": False}
            for t in tools]
    history = history if history is not None else []
    history.append(oai.user_message(*user_parts))
    for step in range(max_steps):
        if stop is not None and stop.is_set():
            return "ERROR: cancelled by the orchestrator"
        pending = _mailbox(ctx)
        if pending:
            history.append(oai.user_message(oai.text_input(pending)))
        kwargs: dict[str, Any] = {"model": cfg["model"], "input": history, "instructions": instructions,
                                  "store": False, "include": ["reasoning.encrypted_content"]}
        if defs:
            kwargs["tools"] = defs
        if cfg.get("effort"):
            kwargs["reasoning"] = {"effort": cfg["effort"]}
        if cfg.get("service_tier"):
            kwargs["service_tier"] = cfg["service_tier"]
        t0 = time.time()
        resp = _retry(lambda kw=kwargs: client.responses.create(**kw), stop=stop)
        gw.record(slot_name=ctx.role, provider="openai", model=cfg["model"], purpose=f"agent:{ctx.agent}",
                  request={"step": step, "n_items": len(history)}, response_text=resp.output_text or "",
                  usage=resp.usage.model_dump() if resp.usage else {}, seconds=time.time() - t0, ok=True)
        history += oai.replay_items(resp.output)
        if ctx.role == "job_lead":
            for item in resp.output:
                if getattr(item, "type", "") == "reasoning":
                    text = "\n".join(getattr(part, "text", "") for part in getattr(item, "summary", []) or [])
                    if text:
                        emit(ctx.ws, "thinking", {"id": f"openai:{item.id}", "source": "openai",
                                                  "status": "completed", "text": text})
            if resp.output_text:
                emit(ctx.ws, "message", {"id": f"openai:{resp.id}", "source": "openai",
                                         "status": "completed", "text": resp.output_text})
            emit(ctx.ws, "usage", {"source": "openai", "scope": "call",
                                   "usage": resp.usage.model_dump() if resp.usage else {}})
        calls = [it for it in resp.output if getattr(it, "type", "") == "function_call"]
        if not calls:
            _log(ctx, step=step, final=(resp.output_text or "")[:4000])
            pending = _mailbox(ctx)
            if pending:
                history.append(oai.user_message(oai.text_input(pending)))
                continue
            return resp.output_text or ""
        images: list[bytes] = []
        for c in calls:
            try:
                args = json.loads(c.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            detail = {"id": f"openai:{c.call_id}", "source": "openai", "name": c.name, "arguments": args}
            if ctx.role == "job_lead":
                emit(ctx.ws, "tool", {**detail, "status": "in_progress"})
            res = registry.call(ctx, c.name, args)
            if ctx.role == "job_lead":
                emit(ctx.ws, "tool", {**detail, "status": "failed" if res.is_error else "completed",
                                      "output": res.text[:MAX_TOOL_CHARS]})
            _log(ctx, step=step, tool=c.name, args=args, ok=not res.is_error, out=res.text[:1500])
            history.append({"type": "function_call_output", "call_id": c.call_id, "output": res.text[:MAX_TOOL_TEXT]})
            images += res.images[:6]
        if images:
            history.append(oai.user_message(*[oai.image_input(img, mime=_mime(img)) for img in images],
                                            oai.text_input("Images returned by the tool calls above, in order.")))
    return "ERROR: step budget exhausted without a final answer"


def _mime(img: bytes) -> str:
    return "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"


def _schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini's function schemas reject a few JSON Schema keywords; drop them recursively."""
    if isinstance(schema, dict):
        return {k: _schema_for_gemini(v) for k, v in schema.items() if k not in ("additionalProperties", "$schema")}
    if isinstance(schema, list):
        return [_schema_for_gemini(v) for v in schema]
    return schema


def gemini_loop(gw: Gateway, cfg: dict[str, Any], system: str, parts: list[Any], tools: list[Tool],
                registry: Registry, ctx: ToolContext, max_steps: int = 60, stop: threading.Event | None = None,
                history: list[Any] | None = None) -> str:
    from google.genai import types

    client = gw.google()
    decls = [types.FunctionDeclaration(name=t.name, description=t.description,
                                       parameters_json_schema=_schema_for_gemini(t.schema)) for t in tools]
    tool_cfg = [types.Tool(function_declarations=decls)] if decls else None
    history = history if history is not None else []
    history.append(types.Content(role="user", parts=[p if isinstance(p, types.Part) else types.Part(text=p) for p in parts]))
    for step in range(max_steps):
        if stop is not None and stop.is_set():
            return "ERROR: cancelled by the orchestrator"
        pending = _mailbox(ctx)
        if pending:
            history.append(types.Content(role="user", parts=[types.Part(text=pending)]))
        t0 = time.time()
        resp = _retry(lambda: client.models.generate_content(
            model=cfg["model"], contents=history, config=gem.gen_config(cfg, system=system, tools=tool_cfg)), stop=stop)
        cand = resp.candidates[0].content if resp.candidates else None
        gw.record(slot_name=ctx.role, provider="google", model=cfg["model"], purpose=f"agent:{ctx.agent}",
                  request={"step": step, "turns": len(history)}, response_text=resp.text or "",
                  usage=resp.usage_metadata.model_dump(exclude_none=True) if resp.usage_metadata else {},
                  seconds=time.time() - t0, ok=True)
        if cand is None:
            return resp.text or ""
        history.append(cand)  # keeps thought signatures, which Gemini needs on the next turn
        calls = [p.function_call for p in (cand.parts or []) if p.function_call]
        if not calls:
            _log(ctx, step=step, final=(resp.text or "")[:4000])
            pending = _mailbox(ctx)
            if pending:
                history.append(types.Content(role="user", parts=[types.Part(text=pending)]))
                continue
            return resp.text or ""
        reply_parts: list[Any] = []
        for fc in calls:
            args = dict(fc.args or {})
            res = registry.call(ctx, fc.name, args)
            _log(ctx, step=step, tool=fc.name, args=args, ok=not res.is_error, out=res.text[:1500])
            reply_parts.append(types.Part.from_function_response(name=fc.name, response={"result": res.text[:MAX_TOOL_TEXT]}))
            for img in res.images[:6]:
                reply_parts.append(types.Part.from_bytes(data=img, mime_type=_mime(img)))
        history.append(types.Content(role="user", parts=reply_parts))
    return "ERROR: step budget exhausted without a final answer"


def _mailbox(ctx: ToolContext) -> str:
    if ctx.swarm is None or not hasattr(ctx.swarm, "take_messages"):
        return ""
    from vwb.agents.swarm import message_text
    return message_text(ctx.swarm.take_messages(ctx.agent))


def _retry(fn, attempts: int = 5, stop: threading.Event | None = None):
    for i in range(attempts):
        if stop is not None and stop.is_set():
            raise InterruptedError("agent stopped before model call")
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if i == attempts - 1 or not any(s in msg for s in ("429", "500", "502", "503", "504", "overloaded",
                                                                  "RESOURCE_EXHAUSTED", "UNAVAILABLE", "timeout", "Timeout")):
                raise
            wait = min(60, 5 * 2 ** i)
            log.warning("model call failed (%s); retrying in %ss", msg[:120], wait)
            if stop is None:
                time.sleep(wait)
            else:
                stop.wait(wait)


def run_digest(text: str) -> str:
    return sha256_text(text)[:12]
