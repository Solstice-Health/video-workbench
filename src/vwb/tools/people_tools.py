"""human.ask and the swarm: asking a person, and handing work to workers."""

from __future__ import annotations

from typing import Any

from vwb.ask.broker import Broker
from vwb.tools.base import Tool, ToolContext, ToolResult, parse_json_arg, schema


def human_ask(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    broker = Broker(ctx.ws)
    mode = a.get("mode", "non_blocking")
    t = broker.ask(asker=ctx.agent, question=a["question"], why=a.get("why", ""),
                           options=parse_json_arg(a["options"]), recommended=a["recommended"], kind=a.get("kind", "choose"),
                           lane=a.get("lane", "editor"), scope=a.get("scope", "request"), key=a.get("key"),
                           parks=a.get("parks"), deadline_s=float(a.get("deadline_s", 1800)), mode=mode)
    if mode == "blocking" and t.get("status") == "open":
        t = broker.wait(t["id"], asker=ctx.agent, timeout_s=float(a.get("wait_timeout_s", 1800)),
                        cancel=ctx.state.get("cancel_event") or getattr(ctx.swarm, "stop", None))
    if t.get("status") in {"answered_from_memory", "answered"}:
        note = "The answer is recorded; use it."
    elif mode == "blocking" or t.get("on_timeout") == "keep_parked":
        note = "No answer was received. Keep the dependent action parked. You may do independent work or use human_wait."
    else:
        note = ("Continue independent work or use the recommended option for this reversible choice, "
                "keeping the alternative cheap to switch to. Check human_answers for a later response.")
    return ToolResult.json({**t, "note": note})


def human_wait(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    t = Broker(ctx.ws).wait(a["ticket"], asker=ctx.agent, timeout_s=float(a.get("timeout_s", 1800)),
                            cancel=ctx.state.get("cancel_event") or getattr(ctx.swarm, "stop", None))
    return ToolResult.json({**t, "note": "A timeout or cancellation is not an answer; leave dependent work parked."})


def human_answers(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    ts = Broker(ctx.ws).tickets()
    ids = set(a.get("tickets") or [])
    if ids:
        ts = [t for t in ts if t["id"] in ids]
    return ToolResult.json({"tickets": [{k: t[k] for k in ("id", "status", "question", "recommended", "answer", "kind", "lane")}
                                        for t in ts]})


def swarm_dispatch(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    if ctx.swarm is None:
        return ToolResult.error("no swarm in this session")
    ticket = ctx.swarm.dispatch(sender=ctx.agent, role=a["role"], task=a["task"], context=parse_json_arg(a.get("context")))
    return ToolResult.json({"ticket": ticket, "note": "Work runs in the background. Do other work, then swarm_wait."})


def swarm_wait(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    if ctx.swarm is None:
        return ToolResult.error("no swarm in this session")
    results = ctx.swarm.wait(a.get("tickets") or [], timeout_s=float(a.get("timeout_s", 1800)), actor=ctx.agent)
    return ToolResult.json({"results": results})


TOOLS = [
    Tool("human_ask", "Ask a person a decision that is theirs (taste, brand, rights, a source file, what the brief "
         "means). Two to four options with a recommended one. Choose mode=non_blocking (default) to continue working, "
         "or blocking to await the answer in this tool call. Blocking waits are cancellable and never assume an answer. "
         "Non-blocking reversible choices may default at the deadline; supply/allow never default. Memory and duplicate "
         "merging apply in both modes. kind: choose|supply|allow|confirm|clarify. lane: editor|brief.",
         schema({"question": {"type": "string"}, "why": {"type": "string"},
                 "options": {"type": "array", "items": {"type": "object", "properties": {
                     "id": {"type": "string"}, "label": {"type": "string"}, "consequence": {"type": "string"}},
                     "required": ["id", "label"]}},
                 "recommended": {"type": "string"}, "kind": {"type": "string"}, "lane": {"type": "string"},
                 "scope": {"type": "string"}, "key": {"type": "string"}, "parks": {"type": "string"},
                 "deadline_s": {"type": "number"},
                 "mode": {"type": "string", "enum": ["non_blocking", "blocking"]},
                 "wait_timeout_s": {"type": "number", "minimum": 0, "maximum": 3600}}, ["question", "options", "recommended"]),
         human_ask, "human.ask", "human"),
    Tool("human_wait", "Wait for an existing question's answer. Only the calling agent waits. Cancellable; timeout "
         "leaves it open without choosing an option. Use after finishing independent work on a non-blocking question.",
         schema({"ticket": {"type": "string"}, "timeout_s": {"type": "number", "minimum": 0, "maximum": 3600}}, ["ticket"]),
         human_wait, "human.ask", "human"),
    Tool("human_answers", "Status and answers of your questions to people.",
         schema({"tickets": {"type": "array", "items": {"type": "string"}}}), human_answers, "tool", "human"),
    Tool("swarm_dispatch", "Hand a task to a worker; it runs in the background. role: scene_miner (Gemini, watches the "
         "source natively: moments, hooks, visual hazards), planner_b (GPT-6.1-Sol in Codex: an independent plan from "
         "another model family), composer (GPT-6.1-Sol in Codex: designs and writes one output's canvas file in the "
         "shared work folder, checks and previews it; you review and commit), critic (GPT-6.1-Sol in Codex: checks "
         "renders against its own checklist, ranks, never certifies). Workers get the same toolkit, limited by role.",
         schema({"role": {"type": "string", "enum": ["scene_miner", "planner_b", "composer", "critic"]}, "task": {"type": "string"},
                 "context": {"type": "object"}}, ["role", "task"]), swarm_dispatch, "agent.manage", "swarm"),
    Tool("swarm_wait", "Wait for workers' results (your direct children if none given), up to 60 seconds total. "
         "Incoming messages wake you early. Inspect returned statuses and wait again if needed; a return is not completion.",
         schema({"tickets": {"type": "array", "items": {"type": "string"}}, "timeout_s": {"type": "number"}}),
         swarm_wait, "agent.manage", "swarm"),
]
