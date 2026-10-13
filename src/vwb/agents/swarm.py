"""swarmd-lite: a plaintext, hash-chained mailbox and background workers (spec D07).

The job lead assigns a task; a worker runs in the background with the same toolkit (limited
by its role) and posts a result. Messages are data, never instructions: a worker's result
cannot accept, waive or grant anything (spec: no channel carries authority).
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from vwb.agents import host_codex, loops
from vwb.config import PROMPTS_DIR, slot
from vwb.gateway import Gateway
from vwb.gateway import gemini as gem
from vwb.gateway.core import parse_json_loose
from vwb.tools.base import Registry, ToolContext
from vwb.util.hashing import new_id, sha256_text
from vwb.util.log import log
from vwb.workspace import Workspace, flags
from vwb.workspace.ledger import Ledger

ROLE_SLOT = {"scene_miner": "worker.video", "planner_b": "worker.reason", "composer": "worker.reason", "critic": "critic"}
CODEX_ROLES = {"planner_b", "composer", "critic"}   # GPT-6.1-Sol roles that run in Codex containers when enabled


def prompt(role: str) -> str:
    return (PROMPTS_DIR / f"{role}.md").read_text()


class Swarm:
    def __init__(self, ws: Workspace, registry: Registry, max_workers: int = 6):
        self.ws, self.registry = ws, registry
        self.mailbox = Ledger(ws.swarm / "mailbox.jsonl")
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="worker")
        self.tickets: dict[str, dict[str, Any]] = {}
        self.futures: dict[str, Future] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()        # set on shutdown; loops check it between model turns

    def dispatch(self, sender: str, role: str, task: str, context: Any = None) -> str:
        if role not in ROLE_SLOT:
            raise ValueError(f"unknown worker role {role!r}; one of {sorted(ROLE_SLOT)}")
        ticket = new_id(role, 3)
        self.mailbox.append("assign", sender, {"ticket": ticket, "to": ticket, "role": role, "gist": task[:240],
                                               "payload_sha256": sha256_text(task + repr(context))})
        with self.lock:
            self.tickets[ticket] = {"ticket": ticket, "role": role, "status": "running", "result": None}
            self.futures[ticket] = self.pool.submit(self._run, ticket, sender, role, task, context)
        log.info("swarm: %s -> %s (%s)", sender, ticket, task[:80])
        return ticket

    def _run(self, ticket: str, sender: str, role: str, task: str, context: Any) -> None:
        try:
            text = run_worker(self.ws, self.registry, role, ticket, task, context, stop=self.stop)
            try:
                data = parse_json_loose(text)
            except ValueError:
                data = None
            status = "done"
        except Exception as exc:  # a failed worker is a result, not a crash
            text, data, status = f"ERROR: {type(exc).__name__}: {exc}", None, "failed"
            f = flags.make("RUN-DEGRADED", f"Worker {ticket} ({role}) failed: {exc}", subject=f"worker:{ticket}",
                           raised_by="swarm")
            Ledger(self.ws.ledger / "ledger.jsonl").append("flag.raised", "swarm", f.to_dict())
        self.mailbox.append("result", ticket, {"ticket": ticket, "to": sender, "status": status,
                                               "gist": text[:240], "payload_sha256": sha256_text(text)})
        with self.lock:
            self.tickets[ticket].update(status=status, result=data if data is not None else text)
        log.info("swarm: %s %s", ticket, status)

    def wait(self, tickets: list[str], timeout_s: float = 1800) -> list[dict[str, Any]]:
        ids = tickets or [t for t, v in self.tickets.items()]
        for t in ids:
            fut = self.futures.get(t)
            if fut is not None:
                try:
                    fut.result(timeout=timeout_s)
                except Exception:  # noqa: BLE001 - recorded by _run
                    pass
        with self.lock:
            return [dict(self.tickets[t]) for t in ids if t in self.tickets]

    def shutdown(self, grace_s: float = 20) -> None:
        """End of the job lead's turn: stop every running worker (its container too) before the
        tool and model services it depends on go away."""
        self.stop.set()
        with self.lock:
            running = [t for t, v in self.tickets.items() if v["status"] == "running"]
        for t in running:
            host_codex.kill(t)
            self.mailbox.append("cancel", "orchestrator", {"ticket": t, "reason": "job lead turn ended"})
        for t in running:
            try:
                self.futures[t].result(timeout=grace_s)
            except Exception:  # noqa: BLE001 - recorded by _run
                pass
        self.pool.shutdown(wait=False, cancel_futures=True)


def run_worker(ws: Workspace, registry: Registry, role: str, ticket: str, task: str, context: Any,
               stop: threading.Event | None = None) -> str:
    cfg = slot(ROLE_SLOT[role])
    gw = Gateway(ws, actor=ticket)
    ctx = ToolContext(ws=ws, agent=ticket, role=role, gateway=gw)
    tools = registry.for_role(role)
    req = ws.load()
    brief = (f"The person's request: \"{req.message}\"\n"
             + (f"The job lead's reading of it: {req.spec.model_dump_json()}\n" if req.spec else "")
             + f"\nYOUR TASK (from the job lead):\n{task}\n"
             + (f"\nCONTEXT:\n{context}\n" if context else ""))
    if cfg.get("provider") == "google":
        parts: list[Any] = []
        if role == "scene_miner":
            f = gem.upload(gw, Path(req.source().path))
            parts.append(gem.video_part(f, fps=1.0))
        parts.append(brief)
        return loops.gemini_loop(gw, cfg, prompt(role), parts, tools, registry, ctx, stop=stop)
    if role in CODEX_ROLES and host_codex.enabled():
        return host_codex.run_worker(ws, registry, role, ticket, prompt(role) + "\n\n" + brief, cfg)
    return loops.openai_loop(gw, cfg, prompt(role), [{"type": "input_text", "text": brief}], tools, registry, ctx,
                             stop=stop)
