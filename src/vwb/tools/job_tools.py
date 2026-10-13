"""Job, skills, plan and memory tools."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from vwb.config import RECIPES_DIR
from vwb.tools.base import Tool, ToolContext, ToolResult, schema
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import flags
from vwb.workspace.request import AssumedDefault, JobSpec


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def job_spec(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    spec = JobSpec(summary=a.get("summary", ""), outputs=a.get("outputs"), profile=a.get("profile"),
                   recipes=a.get("recipes", []), duration_s=a.get("duration_s"), constraints=a.get("constraints", []),
                   assumed_defaults=[AssumedDefault(**d) for d in a.get("assumed_defaults", [])])
    req = ctx.ws.load()
    req.spec = spec
    req.status = "working"
    ctx.ws.save(req)
    for d in spec.assumed_defaults:
        f = flags.make("ASSUMED-DEFAULT", f"{d.item}: {d.value} ({d.why})", subject=f"default:{d.item}", raised_by=ctx.agent)
        ctx.ledger.append("flag.raised", ctx.agent, f.to_dict())
    ctx.ledger.append("job.spec", ctx.agent, spec.model_dump())
    return ToolResult.json({"saved": True, "spec": spec.model_dump(),
                            "note": "Each assumed default is recorded as ASSUMED-DEFAULT (INFO)."})


def skill_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    skills = []
    for p in sorted(RECIPES_DIR.glob("*/SKILL.md")):
        fm = _frontmatter(p.read_text())
        skills.append({"name": fm.get("name", p.parent.name), "description": fm.get("description", "")})
    return ToolResult.json({"skills": skills, "note": "Recipes advise; checks decide. Read one with skill_read."})


def skill_read(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    name = re.sub(r"[^a-z0-9_-]", "", a["name"].lower())
    p = RECIPES_DIR / name / "SKILL.md"
    if not p.is_file():
        return ToolResult.error(f"no skill {name!r}; call skill_list")
    ctx.ledger.append("skill.read", ctx.agent, {"skill": name})
    return ToolResult(text=p.read_text())


def plan_update(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    """Codex-style update_plan: the plan is the checkpoint a FULL wake restarts from."""
    plan = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": ctx.agent,
            "explanation": a.get("explanation", ""), "steps": a["steps"]}
    write_json(ctx.ws.memory / "plan.json", plan)
    ctx.ledger.append("plan.update", ctx.agent, {"steps": len(a["steps"]),
                                                  "done": sum(1 for s in a["steps"] if s.get("status") == "done")})
    return ToolResult.json({"saved": True, "steps": len(a["steps"])})


def memory_note(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    path = ctx.ws.memory / "notes.md"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a") as fh:
        fh.write(f"- [{stamp}] ({ctx.agent}) {a['text'].strip()}\n")
    return ToolResult.json({"saved": True})


def memory_read(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    notes = (ctx.ws.memory / "notes.md").read_text() if (ctx.ws.memory / "notes.md").exists() else ""
    plan = read_json(ctx.ws.memory / "plan.json", {})
    return ToolResult.json({"plan": plan, "notes": notes[-6000:]})


TOOLS = [
    Tool("job_spec", "Record what you understood the request to be: summary, number of outputs, platform profile "
         "(e.g. youtube_shorts), recipes to chain (e.g. [\"clip\", \"remake\"], [] for open mode), duration range in "
         "seconds, constraints, and every default you assumed (each becomes an ASSUMED-DEFAULT flag).",
         schema({"summary": {"type": "string"}, "outputs": {"type": "integer"}, "profile": {"type": "string"},
                 "recipes": {"type": "array", "items": {"type": "string"}},
                 "duration_s": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                 "constraints": {"type": "array", "items": {"type": "string"}},
                 "assumed_defaults": {"type": "array", "items": {"type": "object", "properties": {
                     "item": {"type": "string"}, "value": {}, "why": {"type": "string"}}, "required": ["item", "value"]}}},
                ["summary"]), job_spec, "tool", "job"),
    Tool("skill_list", "List recipes (skills) for request types. Each is a SKILL.md procedure.", schema({}),
         skill_list, "tool", "job"),
    Tool("skill_read", "Read one recipe (SKILL.md) in full.", schema({"name": {"type": "string"}}, ["name"]),
         skill_read, "tool", "job"),
    Tool("plan_update", "Save your plan as steps with status pending|in_progress|done. It is your checkpoint: a fresh "
         "session resumes from it.", schema({"explanation": {"type": "string"}, "steps": {"type": "array", "items": {
             "type": "object", "properties": {"step": {"type": "string"}, "status": {"type": "string",
                                                                                       "enum": ["pending", "in_progress", "done"]}},
             "required": ["step", "status"]}}}, ["steps"]), plan_update, "tool", "job"),
    Tool("memory_note", "Append a short note to job memory (decisions, lessons, what to do next).",
         schema({"text": {"type": "string"}}, ["text"]), memory_note, "tool", "job"),
    Tool("memory_read", "Read the saved plan and job memory notes.", schema({}), memory_read, "tool", "job"),
]
