"""Typed tools, the policy dial and the tool gateway.

Every agent host (Claude Agent SDK, the harness's own tool loop for GPT and Gemini, an
external MCP client) reaches the toolkit through `Registry.call`. That single path checks
the role allowlist and the policy dial, logs the call with input and output hashes
(invariant I4), and runs the loop guards. A tool never trusts its caller's claims.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vwb.config import load_config
from vwb.gateway import Gateway
from vwb.util.hashing import sha256_bytes, sha256_text
from vwb.util.jsonio import canonical_json, dumps_compact
from vwb.workspace import Workspace
from vwb.workspace.ledger import CallLog, Ledger

LOOP_GUARD_CALLS = 25          # calls with no new artifact before a notice (spec: loop guards)
RESTART_CALLS = 150            # calls before a worker should restart (RUN-DEGRADED)
MAX_RESULT_CHARS = 60_000      # longer results spill to work/.results/ and the agent pages through them
SPILL_HEAD_CHARS = 16_000
MAX_IMAGES = 8                 # images per result; each is bounded by media.frames.fit_image


@dataclass
class ToolResult:
    text: str = ""
    images: list[bytes] = field(default_factory=list)       # PNG or JPEG bytes
    data: Any = None
    is_error: bool = False
    artifacts: list[str] = field(default_factory=list)       # content hashes of things the call created

    @staticmethod
    def json(obj: Any, images: list[bytes] | None = None, artifacts: list[str] | None = None) -> ToolResult:
        return ToolResult(text=dumps_compact(obj), images=images or [], data=obj, artifacts=artifacts or [])

    @staticmethod
    def error(message: str) -> ToolResult:
        return ToolResult(text=f"ERROR: {message}", is_error=True)


@dataclass
class ToolContext:
    ws: Workspace
    agent: str                     # e.g. job_lead, scene_miner-1
    role: str                      # job_lead | scene_miner | planner_b | critic
    gateway: Gateway
    swarm: Any = None              # set by the orchestrator for roles that may dispatch
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.ws.ledger / "ledger.jsonl")


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], ToolResult]
    capability: str = "tool"       # the policy-dial row that governs this tool
    area: str = "misc"


class Registry:
    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}
        self._guards: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ---- policy
    @staticmethod
    def allowed_for_role(role: str, name: str) -> bool:
        roles = load_config("policy").get("roles", {})
        spec = roles.get(role)
        if spec is None:
            return False
        allow = spec.get("allow", [])
        return ("*" in allow or name in allow) and name not in spec.get("deny", [])

    @staticmethod
    def mode(capability: str) -> str:
        return load_config("policy").get("capabilities", {}).get(capability, "allow")

    def for_role(self, role: str) -> list[Tool]:
        return [t for t in self.tools.values() if self.allowed_for_role(role, t.name)]

    # ---- the one call path
    def call(self, ctx: ToolContext, name: str, args: dict[str, Any] | None) -> ToolResult:
        args = args or {}
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult.error(f"unknown tool {name!r}")
        if not self.allowed_for_role(ctx.role, name):
            return ToolResult.error(f"role {ctx.role} may not call {name} (config/policy.yaml roles)")
        if ctx.swarm is not None and hasattr(ctx.swarm, "prepare_context"):
            ctx.swarm.prepare_context(ctx)
        inherited = ctx.state.get("allowed_tools")
        if inherited is not None and name not in inherited:
            return ToolResult.error(f"{name} is outside this agent's inherited tool permissions")
        cancel = ctx.state.get("cancel_event")
        if cancel is not None and cancel.is_set():
            return ToolResult.error("agent interrupted; this tool call was not executed")
        mode = self.mode(tool.capability)
        if mode == "block":
            return ToolResult.error(f"{name} is blocked by the policy dial ({tool.capability}: block)")
        if mode == "ask":
            from vwb.ask.broker import Broker
            ticket = Broker(ctx.ws).ask(asker=ctx.agent, question=f"May {ctx.agent} run {name}?", why=f"capability {tool.capability} is set to ask",
                                        options=[{"id": "grant", "label": "Allow"}, {"id": "deny", "label": "Do not allow"}],
                                        recommended="deny", kind="allow", lane="editor", scope="request",
                                        key=f"allow:{tool.capability}", on_timeout="keep_parked")
            answer = ticket.get("answer") or {}
            if ticket.get("status") == "answered_from_memory" and answer.get("option") == "grant":
                pass  # A signed answer remembered for this request grants this capability.
            elif ticket.get("status") == "answered_from_memory" and answer.get("option") == "deny":
                return ToolResult.error(f"{tool.capability} was denied by the person for this request")
            else:
                return ToolResult.json({"parked": True, "ticket": ticket["id"],
                                        "note": "A person must grant this capability; the call is parked, the request continues."})
        t0 = time.time()
        try:
            if ctx.swarm is not None and hasattr(ctx.swarm, "activity"):
                ctx.swarm.activity(ctx.agent, name)
            result = tool.handler(ctx, args)
        except Exception as exc:  # a tool failure is data for the agent, never a crash of the harness
            result = ToolResult.error(f"{type(exc).__name__}: {exc}")
            result.data = {"traceback": traceback.format_exc()[-2000:]}
        seconds = time.time() - t0
        if ctx.swarm is not None and hasattr(ctx.swarm, "tool_event"):
            ctx.swarm.tool_event(ctx.agent, name, result.text, result.is_error)
        if ctx.swarm is not None and name != "agent_inbox" and hasattr(ctx.swarm, "take_messages"):
            from vwb.agents.swarm import message_text
            mailbox = message_text(ctx.swarm.take_messages(ctx.agent))
            if mailbox:
                result.text += "\n\n" + mailbox
        self._bound(ctx, name, result)
        if mode == "flag":
            from vwb.workspace import flags
            f = flags.make("POLICY-USED", f"{ctx.agent} used {name} ({tool.capability}: flag)", subject=f"{tool.capability}:{name}",
                           raised_by="gateway")
            ctx.ledger.append("flag.raised", "gateway", f.to_dict())
        out_hash = sha256_text(result.text + "".join(sha256_bytes(i) for i in result.images))
        CallLog(ctx.ws.ledger / "tools.jsonl").record(
            actor=ctx.agent, role=ctx.role, tool=name, input_sha256=sha256_text(canonical_json(args)),
            output_sha256=out_hash, seconds=round(seconds, 3), ok=not result.is_error, kind="tool")
        notice = self._guard(ctx.agent, result)
        if notice:
            result.text += f"\n\nNOTICE: {notice}"
        return result

    @staticmethod
    def _bound(ctx: ToolContext, name: str, result: ToolResult) -> None:
        """Keep every result within what any host can carry: bounded images, and long text saved to a
        file in work/ with its head returned, so the agent reads the rest in slices (file_read)."""
        from vwb.media.frames import fit_image
        result.images = [fit_image(i) for i in result.images[:MAX_IMAGES]]
        if len(result.text) <= MAX_RESULT_CHARS:
            return
        rel = f".results/{ctx.agent}/{time.strftime('%H%M%S')}-{int(time.time() * 1000) % 1000:03d}-{name}.txt"
        path = ctx.ws.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.text)
        n_lines = result.text.count("\n") + 1
        head = result.text[:SPILL_HEAD_CHARS]
        head = head[: head.rfind("\n")] if "\n" in head else head
        notice = (f"[TRUNCATED: {len(result.text):,} chars, {n_lines:,} lines. The full result is in work file {rel}; "
                  "read it in slices with file_read(path, offset, limit), or narrow the call.]")
        result.text = f"{notice}\n{head}\n\n{notice}"

    def _guard(self, agent: str, result: ToolResult) -> str | None:
        with self._lock:
            g = self._guards.setdefault(agent, {"calls": 0, "since_new": 0, "seen": set()})
            g["calls"] += 1
            new = [a for a in result.artifacts if a not in g["seen"]]
            g["seen"].update(new)
            g["since_new"] = 0 if new else g["since_new"] + 1
            if g["since_new"] and g["since_new"] % LOOP_GUARD_CALLS == 0:
                return (f"loop guard: {g['since_new']} calls without a new artifact. Change approach, render something, "
                        "or finish with what you have.")
            if g["calls"] == RESTART_CALLS:
                return "budget notice: 150 tool calls in this session. Checkpoint your plan with plan_update."
        return None


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def parse_json_arg(value: Any) -> Any:
    """Some hosts pass nested objects as JSON strings; accept both."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value
