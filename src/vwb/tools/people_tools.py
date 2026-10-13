"""human.ask and the swarm: asking a person, and handing work to workers."""

from __future__ import annotations

from typing import Any

from vwb.ask.broker import Broker
from vwb.tools.base import Tool, ToolContext, ToolResult, parse_json_arg, schema


def human_ask(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    t = Broker(ctx.ws).ask(asker=ctx.agent, question=a["question"], why=a.get("why", ""),
                           options=parse_json_arg(a["options"]), recommended=a["recommended"], kind=a.get("kind", "choose"),
                           lane=a.get("lane", "editor"), scope=a.get("scope", "request"), key=a.get("key"),
                           parks=a.get("parks"), deadline_s=float(a.get("deadline_s", 1800)))
    note = ("Answered from memory; use it." if t.get("status") == "answered_from_memory" else
            "Never wait: go ahead with the recommended option now (it is recorded as ASSUMED-DEFAULT if no one answers), "
            "and keep the alternative cheap to switch to.")
    return ToolResult.json({**t, "note": note})


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
    results = ctx.swarm.wait(a.get("tickets") or [], timeout_s=float(a.get("timeout_s", 1800)))
    return ToolResult.json({"results": results})


TOOLS = [
    Tool("human_ask", "Ask a person a decision that is theirs (taste, brand, rights, a source file, what the brief "
         "means). Two to four options with a recommended one. It never blocks: the broker answers from memory, merges "
         "duplicates, and defaults to the recommended option at the deadline. kind: choose|supply|allow|confirm|clarify. "
         "lane: editor (production choice) | brief (what the brief means). Job lead only.",
         schema({"question": {"type": "string"}, "why": {"type": "string"},
                 "options": {"type": "array", "items": {"type": "object", "properties": {
                     "id": {"type": "string"}, "label": {"type": "string"}, "consequence": {"type": "string"}},
                     "required": ["id", "label"]}},
                 "recommended": {"type": "string"}, "kind": {"type": "string"}, "lane": {"type": "string"},
                 "scope": {"type": "string"}, "key": {"type": "string"}, "parks": {"type": "string"},
                 "deadline_s": {"type": "number"}}, ["question", "options", "recommended"]),
         human_ask, "human.ask", "human"),
    Tool("human_answers", "Status and answers of your questions to people.",
         schema({"tickets": {"type": "array", "items": {"type": "string"}}}), human_answers, "tool", "human"),
    Tool("swarm_dispatch", "Hand a task to a worker; it runs in the background. role: scene_miner (Gemini, watches the "
         "source natively: moments, hooks, visual hazards), planner_b (GPT-6.1-Sol in Codex: an independent plan from "
         "another model family), composer (GPT-6.1-Sol in Codex: designs and writes one output's canvas file in the "
         "shared work folder, checks and previews it; you review and commit), critic (GPT-6.1-Sol in Codex: checks "
         "renders against its own checklist, ranks, never certifies). Workers get the same toolkit, limited by role.",
         schema({"role": {"type": "string", "enum": ["scene_miner", "planner_b", "composer", "critic"]}, "task": {"type": "string"},
                 "context": {"type": "object"}}, ["role", "task"]), swarm_dispatch, "model.call", "swarm"),
    Tool("swarm_wait", "Wait for workers' results (all of your open tickets if none given).",
         schema({"tickets": {"type": "array", "items": {"type": "string"}}, "timeout_s": {"type": "number"}}),
         swarm_wait, "tool", "swarm"),
]
