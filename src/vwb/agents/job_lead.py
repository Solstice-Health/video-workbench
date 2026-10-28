"""The main agent (slot `main`), on the host the slot names:

  codex       Codex in a Docker container (host_codex): its own shell and patch tools on the
              work folder, the toolkit over MCP with the swarm attached, the model behind the
              key-holding proxy
  claude_sdk  a Claude Agent SDK session with no built-in tools; the toolkit is an in-process
              MCP server (SDK isolation mode)
  tool_loop   the harness's own stateless Responses API loop

A request is one conversation. On codex and claude_sdk the first message starts a thread and every
later message resumes it with its full history; a host error resumes the thread once. If a host
fails, the slot's next fallback continues on its own thread when it has one (told the turns it
missed), else from a FULL wake (request, plan, outputs, asks), and the gateway records SLOT-FALLBACK.
Every host reaches the same tool gateway.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vwb import prompts
from vwb.agents import attachment_inputs, host_codex, loops, provider_loop
from vwb.agents.events import MAX_TOOL_CHARS, context_usage, emit, set_active_host
from vwb.agents.host_codex import HostUnavailable, ThreadMissing
from vwb.agents.swarm import Swarm
from vwb.config import DOCS_DIR, PROMPTS_DIR, RECIPES_DIR, slot
from vwb.gateway import Gateway
from vwb.models import direct_anthropic_route
from vwb.tools import build_registry
from vwb.tools.base import Registry, ToolContext
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import append_jsonl, read_json, write_json
from vwb.util.log import console, log
from vwb.workspace import Workspace, flags
from vwb.workspace.attachments import prompt as attachment_prompt
from vwb.workspace.checkpoints import read_checkpoint
from vwb.workspace.ledger import Ledger


def system_prompt(cfg: dict[str, Any] | None = None) -> str:
    return prompts.content(cfg)


def wake_context(ws: Workspace, *, full_chat: bool = False) -> str:
    """FULL wake: a stateless respawn reads the request, plan, outputs, asks and notes (spec D05)."""
    req = ws.load()
    full_chat = full_chat or bool(read_checkpoint(ws))
    lines = [f"Request {req.id}. Source media: " + ", ".join(f"{s.ref} ({s.original_path})" for s in req.sources)]
    if not req.sources:
        lines.append("No source video is attached. Build from the brief and authorized design references; use a duration-based canvas, not source cuts.")
    if req.spec:
        lines.append("Recorded job spec: " + req.spec.model_dump_json())
    plan = read_json(ws.memory / "plan.json")
    if plan:
        lines.append("Saved plan: " + json.dumps(plan["steps"]))
    notes = ws.memory / "notes.md"
    if notes.exists():
        lines.append("Job memory notes:\n" + notes.read_text()[-4000:])
    from vwb.canvas.store import CanvasStore
    store = CanvasStore(ws.canvas)
    rows = []
    for out in store.outputs():
        v = store.head(out)
        ver = read_json(ws.qc / out / f"v{v}" / "verification.json", {})
        rows.append(f"{out} v{v} triage={ver.get('triage', 'not submitted')}")
    if rows:
        lines.append("Outputs so far: " + "; ".join(rows))
    tickets = [read_json(p) for p in sorted((ws.asks / "tickets").glob("*.json"))] if (ws.asks / "tickets").exists() else []
    for t in tickets:
        lines.append(f"Question {t['id']} ({t['status']}): {t['question']} -> answer {t.get('answer')}")
    turns = req.chat if full_chat else req.chat[-8:]
    if len(turns) > 1:
        from vwb.workspace.references import prompt as reference_prompt
        lines.append(("Previous public chat:\n" if full_chat else "Recent chat:\n") +
                     "\n".join(f"{t.role}: {t.text if full_chat else t.text[:600]}{reference_prompt(t.context)}{attachment_prompt(t.attachments)}" for t in turns[:-1]))
    lines.append(attachment_inputs.catalog_note(ws))
    return "\n".join(lines)


def _render_result(res) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": res.text or "(no text)"}]
    for img in res.images[:8]:
        mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
        content.append({"type": "image", "data": base64.b64encode(img).decode(), "mimeType": mime})
    return {"content": content, "is_error": res.is_error}


async def _run_claude(ws: Workspace, registry: Registry, swarm: Swarm, prompt_text: str, cfg: dict[str, Any],
                      state: dict[str, Any], attachment_refs: list[dict[str, Any]] | None = None) -> str:
    """One Claude session (or a resume of `state['session']`); records the session id in `state`."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
        create_sdk_mcp_server,
        tool,
    )

    ctx = ToolContext(ws=ws, agent="job_lead", role="job_lead", gateway=Gateway(ws, actor="job_lead"), swarm=swarm)
    ctx.state['cancel_event'] = threading.Event()
    sdk_tools = []
    tool_ids: dict[str, list[str]] = {}
    for t in registry.for_role("job_lead"):
        def make(name: str):
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                res = await asyncio.to_thread(registry.call, ctx, name, args)
                keys = tool_ids.get(name + json.dumps(args, sort_keys=True), [])
                ident = keys.pop(0) if keys else f"claude:tool:{time.time_ns()}"
                emit(ws, "tool", {"id": ident, "source": "claude", "name": name, "arguments": args,
                                  "status": "failed" if res.is_error else "completed",
                                  "output": res.text[:MAX_TOOL_CHARS]})
                return _render_result(res)
            return handler
        sdk_tools.append(tool(t.name, t.description, t.schema)(make(t.name)))
    server = create_sdk_mcp_server(name="vwb", version="1.0", tools=sdk_tools)
    allowed = [f"mcp__vwb__{t.name}" for t in registry.for_role("job_lead")]
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and k != "CLAUDECODE"}
    env["MCP_TOOL_TIMEOUT"] = "5400000"      # batch renders and worker waits can take many minutes
    env["MAX_MCP_OUTPUT_TOKENS"] = "50000"   # the gateway already bounds results (tools.base.MAX_RESULT_CHARS)
    options = ClaudeAgentOptions(
        model=cfg["model"], effort=cfg.get("effort"), system_prompt=system_prompt(cfg),
        mcp_servers={"vwb": server}, strict_mcp_config=True, tools=[], allowed_tools=allowed,
        permission_mode="dontAsk", setting_sources=[], skills=[], cwd=str(ws.work),
        max_turns=int(cfg.get("max_turns", 400)), env=env, resume=state.get("session"),
        max_buffer_size=64 * 1024 * 1024)     # the CLI echoes tool results (with images) back on stdout
    transcript = ws.memory / "job_lead.transcript.jsonl"
    final = ""
    try:
        async with ClaudeSDKClient(options=options) as client:
            if attachment_refs:
                async def prompt_messages():
                    yield {"type": "user", "message": {"role": "user", "content": attachment_inputs.input_parts(
                        ws, prompt_text, attachment_refs, "anthropic")}, "parent_tool_use_id": None}
                await client.query(prompt_messages())
            else:
                await client.query(prompt_text)
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    state["session"] = msg.data.get("session_id") or state.get("session")
                    set_active_host(ws, f"claude_sdk:{cfg['model']}", state.get("session"))
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            console.print(f"[bold cyan]job lead:[/] {block.text.strip()[:1200]}")
                            append_jsonl(transcript, {"ts": time.time(), "text": block.text})
                            emit(ws, "message", {"id": f"claude:{state.get('session')}:{time.time_ns()}",
                                                 "source": "claude", "status": "completed", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            name = block.name.removeprefix("mcp__vwb__")
                            tool_ids.setdefault(name + json.dumps(block.input, sort_keys=True), []).append(f"claude:{block.id}")
                            console.print(f"[dim]  → {name} {json.dumps(block.input)[:160]}[/]")
                            append_jsonl(transcript, {"ts": time.time(), "tool": name, "input": block.input})
                            emit(ws, "tool", {"id": f"claude:{block.id}", "source": "claude", "status": "in_progress",
                                              "name": name, "arguments": block.input})
                elif isinstance(msg, ResultMessage):
                    state["session"] = msg.session_id or state.get("session")
                    final = msg.result or final
                    append_jsonl(transcript, {"ts": time.time(), "result": final, "turns": msg.num_turns,
                                              "cost_usd": msg.total_cost_usd, "session": msg.session_id,
                                              "is_error": msg.is_error})
                    emit(ws, "usage", {"source": "claude", "scope": "session", "usage": msg.usage or {},
                                       "cost_usd": msg.total_cost_usd})
                    if msg.is_error:
                        raise RuntimeError(f"job lead session ended with an error: {final[:400]}")
    finally:
        ctx.state['cancel_event'].set()
    return final


SESSION_FILE = "lead_session.json"
RESUME_NOTE = ("[harness] Your turn stopped on a host error ({error}). Your tool calls up to that point took effect. "
               "Continue from where you stopped; workers you dispatched may still be running (swarm_wait).\n\n"
               "The message you were answering:\n{message}")


def run(ws: Workspace, message: str, first_turn: bool, *, cfg: dict[str, Any] | None = None) -> str:
    """One chat turn. A request is one conversation with the main agent: the first message starts its thread,
    and every later message continues that thread with its full history, like a Cursor or Codex chat.

    memory/lead_session.json keeps, per host and model, the thread id and how many replies of the
    conversation that thread has seen, plus the ledger point of the last message."""
    registry = build_registry()
    swarm = Swarm(ws, registry)
    if cfg is None:
        cfg = prompts.with_snapshot(slot("main"), ws)
    else:
        cfg = dict(cfg)
        # Saved jobs from before profiles existed keep the existing descriptive
        # behavior, regardless of preferences chosen after their submission.
        if "prompt" not in cfg:
            cfg["prompt"] = prompts.snapshot_profile()
        cfg = prompts.with_snapshot(cfg)
    path = ws.memory / SESSION_FILE
    session = read_json(path, {}) or {}
    ledger = Ledger(ws.ledger / "ledger.jsonl")
    checkpoint = read_checkpoint(ws)
    baseline = max(int(session.get("seq", 0)), checkpoint.get("ledger_seq", 0))
    events = human_events(ledger.events(["human."]), baseline)
    turn = ledger.append("chat.turn", "human", {"text": message})
    emit(ws, "turn.started", {"id": f"turn:{turn['seq']}", "text": message})
    try:
        reply = _lead(ws, registry, swarm, message, first_turn, cfg, ledger, session, events)
    except KeyboardInterrupt:
        emit(ws, "turn.stopped", {"id": f"turn:{turn['seq']}", "text": "Stopped. The conversation is saved."})
        raise
    except BaseException as exc:
        emit(ws, "turn.failed", {"id": f"turn:{turn['seq']}", "text": str(exc)[:1000]})
        raise
    finally:
        # Save before potentially slow worker cleanup: another stop signal must not lose
        # the thread ID that this turn created. Cleanup still runs if persistence fails.
        try:
            write_json(path, session)
        finally:
            swarm.shutdown()
    ws.add_chat("job_lead", reply)
    rec = session.get("threads", {}).get(session.get("key") or "", {})
    ledger.append("chat.reply", "job_lead", {"text": reply[:4000], "host": session.get("key"),
                                             "thread": rec.get("thread_id")})
    if rec:
        rec["replies"] = sum(t.role in {"job_lead", "assistant"} for t in ws.load().chat)
    session.update(turns=int(session.get("turns", 0)) + 1, seq=turn["seq"])
    write_json(path, session)
    emit(ws, "turn.completed", {"id": f"turn:{turn['seq']}", "text": reply})
    return reply


def human_events(rows: list[dict[str, Any]], after_seq: int) -> list[str]:
    """What people did outside the chat after a ledger point (answers, acknowledgements, waivers, accepts),
    one line each, so the continued conversation hears about it."""
    out = []
    for e in rows:
        if e["seq"] <= after_seq:
            continue
        who, d, kind = str(e.get("actor", "")).removeprefix("human:"), e.get("data") or {}, e["kind"]
        if kind == "human.answer":
            out.append(f"{who} answered {d.get('ticket')}: {d.get('option')}" + (f" ({d['text']})" if d.get("text") else ""))
        elif kind == "human.ack":
            out.append(f"{who} acknowledged flag {d.get('id')}")
        elif kind == "human.waive":
            out.append(f"{who} waived flag {d.get('id')}: {d.get('reason')}")
        elif kind == "human.accept":
            out.append(f"{who} accepted {e.get('output')} v{e.get('version')}")
        else:
            out.append(f"{who}: {kind}")
    return out


def missed_turns(chat: list[Any], replies_seen: int) -> list[Any]:
    """The turns a thread did not see because another host answered them: everything after the thread's own
    last reply, without the current message (the last turn). Empty when no other host replied."""
    start, seen = 0, 0
    for i, t in enumerate(chat):
        if seen == replies_seen:
            break
        if t.role in {"job_lead", "assistant"}:
            seen, start = seen + 1, i + 1
    missed = chat[start:-1]
    return missed if any(t.role in {"job_lead", "assistant"} for t in missed) else []


def _opening(ws: Workspace, message: str) -> str:
    req = ws.load()
    if not req.sources:
        return (f"{message}\n\n[harness] Request {ws.id}; no source video is attached. Start from the brief and "
                "authorized references (including the selected Figma integration). No video index or source narration "
                "exists. Use canvas_new with duration_s, not source spans; never require a dummy source video.")
    return f"{message}\n\n[harness] Request {ws.id}; source {req.source().ref}; the edit index is built."


def _docs() -> dict[str, Path]:
    docs = {"instructions": PROMPTS_DIR / "job_lead.md", "canvas_schema": DOCS_DIR / "CANVAS.md",
            "code_navigation": PROMPTS_DIR / "code_navigation.md"}
    docs.update({f"recipe:{p.parent.name}": p for p in sorted(RECIPES_DIR.glob("*/SKILL.md"))})
    return docs


def surface(registry: Registry) -> dict[str, dict[str, str]]:
    """What the main agent sees of the harness: each tool (a hash of its description and schema) and each document
    it reads (its instructions, the canvas reference, the recipes). Stored with a thread after every turn."""
    tools = {t.name: sha256_text(t.description + json.dumps(t.schema, sort_keys=True))[:12] for t in registry.for_role("job_lead")}
    return {"tools": tools, "docs": {k: sha256_text(p.read_text())[:12] for k, p in _docs().items()}}


def _prompt_surface(registry: Registry, cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    current = surface(registry)
    return {**current, "docs": {**current["docs"], "instructions": sha256_text(system_prompt(cfg))[:12]}}


def surface_note(old: dict[str, Any] | None, new: dict[str, Any], since: float | None = None,
                 *, cfg: dict[str, Any] | None = None) -> str:
    """Tell a resumed thread what changed in the harness since its last turn, so it does not work from stale
    tool descriptions, canvas reference or recipes. Threads stored before fingerprints existed are compared by
    file time against their last reply (`since`, epoch seconds); their tools cannot be compared."""
    lines: list[str] = []
    old_tools = (old or {}).get("tools", {})
    if old_tools:
        added = sorted(set(new["tools"]) - set(old_tools))
        changed = sorted(k for k in new["tools"] if k in old_tools and old_tools[k] != new["tools"][k])
        removed = sorted(set(old_tools) - set(new["tools"]))
        lines += ([f"new tools: {', '.join(added)}"] if added else []) + \
                 ([f"changed tools (read their descriptions again): {', '.join(changed)}"] if changed else []) + \
                 ([f"removed tools: {', '.join(removed)}"] if removed else [])
    if old:
        stale = [k for k in new["docs"] if old.get("docs", {}).get(k) != new["docs"][k]]
    elif since:
        stale = [k for k, p in _docs().items() if p.stat().st_mtime > since]
    else:
        stale = []
    if not old and cfg and cfg.get("prompt", {}).get("id", prompts.DEFAULT) != prompts.DEFAULT and "instructions" not in stale:
        stale.append("instructions")
    refs = [("canvas_schema" if k == "canvas_schema" else f"skill_read {k.split(':', 1)[1]}") for k in stale if k != "instructions"]
    if refs:
        lines.append("updated references, read them again before relying on them: " + ", ".join(refs))
    note = ("[harness] The toolkit changed since your last turn - " + "; ".join(lines) + ".") if lines else ""
    if "instructions" in stale:
        note += ("\n\n" if note else "") + ("[harness] Your instructions were updated; this version replaces the one at "
                                             "the start of the conversation:\n" + system_prompt(cfg))
    return note


def _last_reply_time(ledger: Ledger) -> float | None:
    from datetime import datetime
    rows = ledger.events(["chat.reply"])
    return datetime.fromisoformat(rows[-1]["ts"]).timestamp() if rows else None


def _continued(ws: Workspace, message: str, events: list[str], replies_seen: int, changes: str = "", *, full_chat: bool = False) -> str:
    """The next message on a thread that holds the conversation: the person's words, plus what the thread missed
    (turns another host answered, harness changes, what people did)."""
    parts = []
    missed = missed_turns(ws.load().chat, replies_seen)
    if missed:
        from vwb.workspace.references import prompt as reference_prompt
        parts.append("[harness] While your host was unavailable, the conversation went on with another host:\n"
                     + "\n".join(f"{t.role}: {t.text if full_chat else t.text[:800]}{reference_prompt(t.context)}{attachment_prompt(t.attachments)}"
                                  for t in (missed if full_chat else missed[-8:])))
    if changes:
        parts.append(changes)
    parts.append(message)
    if events:
        parts.append("[harness] Recorded since the last message: " + "; ".join(events))
    return "\n\n".join(parts)


def _lead(ws: Workspace, registry: Registry, swarm: Swarm, message: str, first_turn: bool, cfg: dict[str, Any],
          ledger: Ledger, session: dict[str, Any], events: list[str]) -> str:
    chain = [cfg] + [dict(cfg, **fb) for fb in cfg.get("fallbacks", [])]
    last: Exception | None = None
    for i, c in enumerate(chain):
        if i > 0:
            log.warning("main agent on %s/%s failed (%s); falling back to %s/%s", chain[i - 1].get("host"),
                        chain[i - 1]["model"], str(last)[:200], c.get("host"), c["model"])
            f = flags.make("SLOT-FALLBACK", f"Main agent served by {c['model']} ({c.get('host')}) instead of {cfg['model']}",
                           subject=f"main:{c.get('host')}:{c['model']}", evidence={"error": str(last)[:400]},
                           raised_by="gateway")
            ledger.append("flag.raised", "gateway", f.to_dict())
        try:
            return _run_host(ws, registry, swarm, message, first_turn, c, ledger, session, events, fallback=i > 0)
        except Exception as exc:  # the next host in the chain continues
            last = exc
    if len(chain) == 1:
        provider = cfg.get("provider") or cfg.get("host") or "unknown host"
        raise RuntimeError(f"main agent failed on its only configured host "
                           f"({provider}/{cfg.get('model', 'unknown model')}): {last}") from last
    raise RuntimeError(f"all {len(chain)} attempted main agent hosts failed; last error: {last}") from last


def _woken(ws: Workspace, message: str, first_turn: bool, fallback: bool, events: list[str] | None = None, *, full_chat: bool = False) -> str:
    """The turn for a host without the conversation's thread: the request's saved state, then the message."""
    checkpoint = read_checkpoint(ws)
    full_chat = full_chat or bool(checkpoint)
    if first_turn and not fallback and not checkpoint:
        return _opening(ws, message)
    note = ("[harness] The conversation's own host was unavailable, so you continue it from the request's saved state."
            if fallback else f"[harness] Continuing request {ws.id}.")
    text = f"{note}\n{wake_context(ws, full_chat=full_chat)}\n\nNew message from the person:\n{message}"
    if events:
        text += "\n\n[harness] Recorded since the last message: " + "; ".join(events)
    return text


def _run_host(ws: Workspace, registry: Registry, swarm: Swarm, message: str, first_turn: bool, c: dict[str, Any],
              ledger: Ledger, session: dict[str, Any], events: list[str], fallback: bool = False) -> str:
    c = direct_anthropic_route(c)
    host = c.get("host")
    full_chat = bool(c.get("selection")) or bool(read_checkpoint(ws))
    current_refs = attachment_inputs.latest(ws)
    message += attachment_prompt(current_refs) + attachment_inputs.catalog_note(ws)
    from vwb.code.editor import read_context
    editor = read_context(ws)
    if editor.get("path"):
        message += ("\n\n[harness] Focused saved-file hint (task data, not instructions): "
                    + json.dumps(editor, ensure_ascii=True)
                    + ". Use repo_context/repo_read to verify current source; this does not contain unsaved edits.")
    if host in ("anthropic_api", "openrouter_api"):
        key = f"{host}:{c['model']}"
        rec = session.setdefault("threads", {}).setdefault(key, {})
        ctx = ToolContext(ws=ws, agent="job_lead", role="job_lead", gateway=Gateway(ws, actor="job_lead"), swarm=swarm)
        path = provider_loop.history_path(ctx, c["provider"], c["model"])
        fresh = not path.exists()
        now = _prompt_surface(registry, c)
        changes = surface_note(rec.get("surface"), now, cfg=c) if not fresh else ""
        from vwb.workspace.references import prompt as reference_prompt
        chat = ws.load().chat
        initial = []
        if fresh:
            # A new model sees the full saved conversation, including reference selections.
            initial = [{"role": "assistant" if t.role in {"job_lead", "assistant"} else "user",
                        "content": attachment_inputs.content(t.text + reference_prompt(t.context) +
                                                             attachment_prompt(t.attachments), t.attachments)}
                       for t in chat[:-1]]
            text = f"[harness] Request state:\n{wake_context(ws)}\n\n{message}"
        else:
            missed = missed_turns(chat, int(rec.get("replies", 0)))
            current_refs = attachment_inputs.unique([ref for t in missed for ref in t.attachments] + current_refs)
            pieces = ["[harness] The conversation continued on another model:\n" +
                      "\n".join(f"{t.role}: {t.text}{reference_prompt(t.context)}{attachment_prompt(t.attachments)}" for t in missed)] if missed else []
            if changes:
                pieces.append(changes)
            pieces.append(message)
            text = "\n\n".join(pieces)
        if events:
            text += "\n\n[harness] Recorded since the last message: " + "; ".join(events)
        rec.update(thread_id=f"api-{path.stem}", config=c)
        session["key"] = key
        set_active_host(ws, key, rec["thread_id"])
        turns = ledger.events(["chat.turn"])
        reply = provider_loop.run(ctx, registry, c, system_prompt(c), text,
                                  turn_id=str(turns[-1]["seq"] if turns else time.time_ns()), initial_messages=initial,
                                  **({"attachment_refs": current_refs} if current_refs else {}))
        rec["surface"] = now
        return reply
    if host == "tool_loop":                        # stateless: every turn starts from the request's saved state
        set_active_host(ws, f"tool_loop:{c['model']}")
        ctx = ToolContext(ws=ws, agent="job_lead", role="job_lead", gateway=Gateway(ws, actor="job_lead"), swarm=swarm)
        text = _woken(ws, message, first_turn, fallback, events)
        reply = loops.openai_loop(ctx.gateway, c, system_prompt(c), attachment_inputs.input_parts(ws, text, current_refs, "openai"),
                                  registry.for_role("job_lead"), registry, ctx, max_steps=int(c.get("max_turns", 400)))
        session["key"] = None
        return reply
    if host not in ("codex", "claude_sdk"):
        raise RuntimeError(f"unknown job lead host {host!r}")
    key = f"{host}:{c['model']}"
    rec = session.setdefault("threads", {}).setdefault(key, {})
    rec["config"] = c
    set_active_host(ws, key, rec.get("thread_id"))
    now = _prompt_surface(registry, c)
    missed = (missed_turns(ws.load().chat, int(rec.get("replies", 0))) if rec.get("thread_id")
              else ws.load().chat[:-1])
    # Recovered/native model switches receive recent pixels as well as paths.
    # Older files remain discoverable through attachment_list/attachment_read.
    current_refs = attachment_inputs.unique([ref for t in missed for ref in t.attachments] + current_refs)[-12:]
    if rec.get("thread_id"):                       # the same conversation: the new message, and what it missed
        changes = surface_note(rec.get("surface"), now, since=None if rec.get("surface") else _last_reply_time(ledger), cfg=c)
        text = _continued(ws, message, events, int(rec.get("replies", 0)), changes, full_chat=full_chat)
    else:
        text = _woken(ws, message, first_turn, fallback, events, full_chat=full_chat)

    if host == "codex":
        def turn(t: str, thread: str | None) -> str:
            prompt = t if thread else system_prompt(c) + "\n\n# The conversation starts\n" + t
            return host_codex.run_lead(ws, registry, swarm, prompt, c, resume=thread, session=rec,
                                       **({"attachment_refs": current_refs} if current_refs else {}))
    else:
        def turn(t: str, thread: str | None) -> str:
            state: dict[str, Any] = {"session": thread}
            try:
                return asyncio.run(_run_claude(ws, registry, swarm, t, c, state,
                                               **({"attachment_refs": current_refs} if current_refs else {})))
            except Exception as exc:
                if thread and re.search(r"no conversation found|session.{0,40}not found", str(exc), re.I):
                    raise ThreadMissing(str(exc)[:400]) from exc
                raise
            finally:
                if state.get("session"):
                    rec["thread_id"] = state["session"]

    reply = _with_resume(turn, text, rec, ledger,
                         fresh=lambda: _woken(ws, message, first_turn, fallback, events, full_chat=bool(c.get("selection"))))
    rec["surface"] = now                           # what this thread has now been told about the harness
    rec["config"] = c                             # recovery can replace the record; preserve compaction settings
    session["key"] = key
    return reply


MAX_FAILED_TURNS = 2       # a thread that fails this many turns in a row is dropped; until then it is resumed


def _with_resume(turn: Callable[[str, str | None], str], text: str, rec: dict[str, Any], ledger: Ledger,
                 fresh: Callable[[], str] | None = None) -> str:
    """Run a turn on the thread in `rec` (a new thread if it has none).

    - The host cannot start (HostUnavailable) or the turn ran out of time: re-raised at once, thread kept, so the
      next host serves this turn and the conversation comes back to this thread next time.
    - The thread is gone from the host's store (ThreadMissing): a new thread starts from the saved state.
    - The turn failed part-way: the same thread is resumed once (its tool calls took effect). A thread that fails
      MAX_FAILED_TURNS turns in a row is dropped; the next turn then starts a new one from a FULL wake."""
    try:
        reply = turn(text, rec.get("thread_id"))
    except (TimeoutError, HostUnavailable):
        raise
    except ThreadMissing as exc:
        log.warning("main agent thread %s is gone (%s); starting a new thread", rec.get("thread_id"), str(exc)[:200])
        ledger.append("host.error", "job_lead", {"error": str(exc)[:400], "thread": rec.get("thread_id"),
                                                 "action": "new_thread"})
        rec.clear()
        reply = turn(fresh() if fresh else text, None)
    except Exception as exc:
        thread = rec.get("thread_id")
        if not thread:
            raise
        log.warning("main agent turn failed (%s); resuming thread %s once", str(exc)[:200], thread)
        ledger.append("host.error", "job_lead", {"error": str(exc)[:400], "thread": thread, "action": "resume"})
        try:
            reply = turn(RESUME_NOTE.format(error=str(exc)[:200], message=text), thread)
        except (TimeoutError, HostUnavailable):
            raise
        except Exception:
            rec["failures"] = int(rec.get("failures", 0)) + 1
            if rec["failures"] >= MAX_FAILED_TURNS:
                ledger.append("host.error", "job_lead", {"thread": thread, "action": "drop_thread",
                                                         "failed_turns": rec["failures"]})
                rec.clear()
            raise
    rec.pop("failures", None)
    return reply


def compact(ws: Workspace) -> dict[str, Any]:
    """Native, in-place Codex compaction. Call in its own process, only when the turn lock is free."""
    from filelock import FileLock, Timeout

    lock = FileLock(str(ws.root / "turn.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise RuntimeError("The agent is working. Queue compaction for after this turn.") from None
    try:
        attachment_inputs.snapshot_catalog(ws)
        state = read_json(ws.memory / SESSION_FILE, {}) or {}
        active = read_json(ws.memory / "active_host.json", {}) or {}
        key = active.get("key") or state.get("key") or ""
        thread_id = active.get("thread_id") or state.get("threads", {}).get(key, {}).get("thread_id")
        if not key.startswith("codex:") or not thread_id:
            raise ValueError("Compaction needs a saved Codex conversation. Send a message with the Codex host first.")
        cfg = dict(state.get("threads", {}).get(key, {}).get("config") or slot("main"), model=key.split(":", 1)[1])
        emit(ws, "compaction.started", {"thread_id": thread_id, "mode": "native"})
        try:
            host_codex.compact_thread(ws, build_registry(), thread_id, cfg)
        except KeyboardInterrupt:
            emit(ws, "compaction.stopped", {"thread_id": thread_id, "mode": "native"})
            raise
        except BaseException as exc:
            emit(ws, "compaction.failed", {"thread_id": thread_id, "mode": "native", "text": str(exc)[:1000]})
            raise
        Ledger(ws.ledger / "ledger.jsonl").append("chat.compacted", "human", {"thread": thread_id, "mode": "native"})
        result = {"supported": True, "mode": "native", "status": "completed", "thread_id": thread_id,
                  "usage": context_usage(ws)}
        emit(ws, "compaction.completed", result)
        return result
    finally:
        lock.release()
