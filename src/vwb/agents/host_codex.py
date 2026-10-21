"""Codex as an agent host, inside Docker (the agent plane). Used for the main agent and the workers.

Each agent session runs `codex exec` in its own container:
  - the container mounts only the request's work/ folder at /workspace, so Codex's own shell
    and apply_patch edit composition files freely, but nothing else on the machine;
  - the model is reached through the control plane's ModelProxy (the real key never enters the
    container; a per-run token does), and every call is logged;
  - the toolkit is reached over MCP at the control plane's ToolServer, limited by the role (the
    job lead's server also carries the swarm, so it can dispatch workers);
  - the container is the sandbox, so Codex runs without its own approval prompts.
The policy dial row `host.codex` turns this host on or off.
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from vwb.agents.events import codex_event, emit, set_active_host
from vwb.gateway import Gateway
from vwb.gateway.services import ModelProxy, ToolServer
from vwb.tools.base import Registry, ToolContext
from vwb.util.jsonio import append_jsonl
from vwb.util.log import console, log
from vwb.workspace import Workspace
from vwb.workspace.ledger import CallLog

IMAGE = "vwb-codex-agent:0.162.1"
TOOL_TIMEOUT_S = 5400            # batch renders, asset generation and worker waits can take many minutes
STREAM_IDLE_TIMEOUT_MS = 3_600_000   # at max effort the model can think for over 30 min before its first token;
                                     # Codex's default (5 min) took that silence for a dead stream and restarted the turn
CONTAINER_NOTES = """
You run inside a sandboxed Linux container. /workspace is the request's shared work folder (other agents use it
too): canvas sources live in outputs/<id>/canvas.json, long tool results in .results/. Your shell cannot reach the
source video, the models or the harness; use the `vwb` MCP tools for the index, frames, assets, advisors, checks,
previews, renders and workers. Python 3 is available: for a canvas of more than a few layers, write a short
generator script (for example outputs/<id>/build.py: shared style dicts, loops over beats and items, timing
computed from the transcript's word times) that writes canvas.json, run it, then canvas_check. Editing and
re-running a script is faster and safer than hand-editing a large JSON file."""
WORKER_END = "\nFinish with the JSON your role asks for, and nothing after it."


class HostUnavailable(RuntimeError):
    """The host could not start a session at all (policy off, Docker or the image missing, the daemon down).
    Nothing ran; the conversation's thread is intact and the next host in the chain serves the turn."""


class ThreadMissing(RuntimeError):
    """The thread to resume is not in the host's store; a new thread has to start from the saved state."""


_DOCKER_DOWN = ("Cannot connect to the Docker daemon", "Error response from daemon", "Unable to find image")


def enabled() -> bool:
    return Registry.mode("host.codex") == "allow" and shutil.which("docker") is not None and _image_ready()


def _image_ready() -> bool:
    proc = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return proc.returncode == 0


def kill(ticket: str) -> None:
    """Stop an agent's container (no-op when it is not running)."""
    if shutil.which("docker"):
        try:
            subprocess.run(["docker", "rm", "-f", f"vwb-{ticket}"], capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("container removal timed out for %s", ticket)


def _ui_job_labels() -> list[str]:
    """Tag only UI-owned containers so forced cancellation cannot stop unrelated agents."""
    job_id = os.environ.get("VWB_UI_JOB_ID", "")
    if len(job_id) == 32 and all(c in "0123456789abcdef" for c in job_id):
        return ["--label", f"vwb.ui-job={job_id}"]
    return []


def run_agent(ws: Workspace, registry: Registry, role: str, ticket: str, prompt: str, cfg: dict[str, Any], *,
              swarm: Any = None, timeout_s: float = 3600, echo: bool = False, codex_home: Path | None = None,
              resume: str | None = None, session: dict[str, Any] | None = None,
              stop: threading.Event | None = None, attachment_refs: list[dict[str, Any]] | None = None) -> str:
    """One Codex turn in a container; returns its final message.

    With codex_home, Codex keeps its threads in that host folder, so a later turn can resume the same
    conversation (resume=thread id) with its full history. The thread id is written to session["thread_id"]."""
    if not enabled():
        raise HostUnavailable("the Codex host is off (policy host.codex), or Docker or the image is missing")
    image_mounts, image_args = _attachment_images(ws, attachment_refs or [])
    if attachment_refs:
        prompt += ("\n[harness] Bounded previews of recent attached images accompany this request when available. "
                   "Use attachment_read for any missing image or an older original.")
    token = pysecrets.token_urlsafe(24)
    ctx = ToolContext(ws=ws, agent=ticket, role=role, gateway=Gateway(ws, actor=ticket), swarm=swarm)
    if swarm is not None:
        swarm.prepare_context(ctx)
    cancel = threading.Event()
    ctx.state["cancel_event"] = cancel
    watcher_done = threading.Event()
    timed_out = threading.Event()
    if role == "job_lead":
        emit(ws, "model.configured", {"source": "codex", "model": cfg["model"], "effort": cfg.get("effort"),
                                      "context_window": cfg.get("context_window"),
                                      "service_tier_requested": cfg.get("service_tier")})
    proxy = ModelProxy(CallLog(ws.ledger / "tools.jsonl"), token=token, actor=ticket,
                       service_tier=cfg.get("service_tier"))
    tools = ToolServer(ctx, registry, role, token)
    out_dir = ws.work / ".codex"
    out_dir.mkdir(parents=True, exist_ok=True)
    last = out_dir / f"{ticket}.md"
    effort = cfg.get("effort", "high")
    provider = (f'model_providers.vwb={{name="vwb gateway", base_url="http://host.docker.internal:{proxy.port}/v1", '
                f'env_key="VWB_TOKEN", wire_api="responses", stream_idle_timeout_ms={STREAM_IDLE_TIMEOUT_MS}}}')
    mcp = (f'mcp_servers.vwb={{url="http://host.docker.internal:{tools.port}/mcp", bearer_token_env_var="VWB_TOKEN", '
           f'startup_timeout_sec=60, tool_timeout_sec={TOOL_TIMEOUT_S}}}')
    mounts = ["-v", f"{ws.work.resolve()}:/workspace", *image_mounts]
    if codex_home is not None:
        codex_home.mkdir(parents=True, exist_ok=True)
        mounts += ["-v", f"{codex_home.resolve()}:/home/agent/.codex"]
    command = ["exec", "resume", resume] if resume else ["exec"]
    cmd = ["docker", "run", "--rm", "-i", "--name", f"vwb-{ticket}", "--cpus", "4", "--memory", "4g",
           *_ui_job_labels(), *mounts, "-e", f"VWB_TOKEN={token}",
           IMAGE, *command, "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
           "-m", cfg["model"], "-c", f'model_reasoning_effort="{effort}"', "-c", 'model_provider="vwb"',
           "-c", 'model_reasoning_summary="auto"',
           "-c", provider, "-c", mcp]
    if cfg.get("context_window"):
        cmd += ["-c", f"model_context_window={int(cfg['context_window'])}"]
    if cfg.get("auto_compact_tokens"):
        cmd += ["-c", f"model_auto_compact_token_limit={int(cfg['auto_compact_tokens'])}"]
    cmd += [*image_args, "-o", f"/workspace/.codex/{ticket}.md", "-"]
    transcript = ws.swarm / f"{ticket}.codex.jsonl"
    t0 = time.time()
    log.info("codex %s (%s, %s) starting in docker", ticket, role, cfg["model"])
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdin is not None and proc.stdout is not None
        def watch() -> None:
            while not watcher_done.wait(0.25):
                if time.time() - t0 > timeout_s:
                    timed_out.set()
                    cancel.set()
                if cancel.is_set() or (stop is not None and stop.is_set()) or (swarm is not None and swarm.stop.is_set()):
                    cancel.set()
                    kill(ticket)
                    if proc.poll() is None:
                        proc.kill()
                    return

        threading.Thread(target=watch, daemon=True, name=f"cancel-{ticket}").start()
        notes = "" if resume else "\n" + CONTAINER_NOTES + (WORKER_END if role not in {"job_lead", "worker"} else "")
        proc.stdin.write(prompt + notes)
        proc.stdin.close()
        final, events, completed, failure = "", 0, False, ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                append_jsonl(transcript, {"raw": line[:2000]})
                continue
            append_jsonl(transcript, ev)
            if role == "job_lead":
                codex_event(ws, ev, ticket)
            elif swarm is not None and ev.get("type") == "item.started":
                item = ev.get("item") or {}
                if item.get("type") == "command_execution":
                    swarm.activity(ticket, "bash")
            if role != "job_lead" and swarm is not None and ev.get("type") == "item.completed":
                child_item = ev.get("item") or {}
                if child_item.get("type") in {"reasoning", "agent_message"} and child_item.get("text"):
                    swarm.progress(ticket, child_item["text"], kind=child_item["type"])
                elif child_item.get("type") == "command_execution":
                    swarm.tool_event(ticket, "bash", str(child_item.get("aggregated_output") or ""),
                                     child_item.get("exit_code", 0) != 0)
            events += 1
            kind = ev.get("type")
            if kind == "thread.started" and session is not None and ev.get("thread_id"):
                session["thread_id"] = ev["thread_id"]
                if role == "job_lead":
                    set_active_host(ws, f"codex:{cfg['model']}", ev["thread_id"])
            elif kind == "turn.completed":
                completed = True
            elif kind in ("turn.failed", "error"):
                failure = str((ev.get("error") or {}).get("message") or ev.get("message") or ev)[:500]
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                final = item.get("text", final)
                if echo and final.strip():
                    console.print(f"[bold cyan]{role}:[/] {final.strip()[:1200]}")
            elif echo and ev.get("type") == "item.started" and item.get("type") == "mcp_tool_call":
                console.print(f"[dim]  → {item.get('tool')} {json.dumps(item.get('arguments'))[:160]}[/]")
            if time.time() - t0 > timeout_s:
                proc.kill()
                raise TimeoutError(f"codex {ticket} ran over {timeout_s}s")
        proc.wait(timeout=60)
        err = proc.stderr.read() if proc.stderr else ""
        if timed_out.is_set():
            raise TimeoutError(f"codex {ticket} ran over {timeout_s}s")
        if cancel.is_set():
            raise InterruptedError(f"codex {ticket} interrupted")
        if not completed:          # a progress message is not a reply: only a completed turn counts
            if events == 0 and resume and "no rollout found" in err:
                raise ThreadMissing(f"thread {resume} is not in the Codex home: {err[-300:]}")
            if events == 0 and any(s in err for s in _DOCKER_DOWN):
                raise HostUnavailable(f"the Codex container did not start: {err[-500:]}")
            raise RuntimeError(f"codex turn did not complete (exit {proc.returncode}): {failure or err[-1500:]}")
        if last.exists():
            final = last.read_text() or final
        log.info("codex %s finished in %.0fs", ticket, time.time() - t0)
        return final
    finally:
        watcher_done.set()
        # Unwind blocking tool calls before shutting down their MCP server.
        cancel.set()
        kill(ticket)
        tools.stop()
        proxy.stop()


def run_worker(ws: Workspace, registry: Registry, role: str, ticket: str, prompt: str, cfg: dict[str, Any],
               timeout_s: float = 3600, **kwargs: Any) -> str:
    return run_agent(ws, registry, role, ticket, prompt, cfg, timeout_s=timeout_s, **kwargs)


def _attachment_images(ws: Workspace, refs: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    from vwb.agents.attachment_inputs import MAX_INLINE_BYTES
    from vwb.workspace.attachments import image_payload

    mounts, args = [], []
    size = 0
    for ref in reversed(refs):
        if ref.get("kind") != "image":
            continue
        try:
            payload = image_payload(ws, ref)
        except (OSError, ValueError):
            continue  # durable path + attachment_read remain in the prompt
        if payload:
            added = len(payload['base64']) * 3 // 4
            if size + added > MAX_INLINE_BYTES:
                continue
            size += added
            target = f"/vwb-attachments/{ref['id']}/{Path(payload['path']).name}"
            mounts[0:0] = ["-v", f"{payload['path']}:{target}:ro"]
            args[0:0] = ["--image", target]
    return mounts, args


def run_lead(ws: Workspace, registry: Registry, swarm: Any, prompt: str, cfg: dict[str, Any],
             timeout_s: float = 6 * 3600, resume: str | None = None,
             session: dict[str, Any] | None = None, attachment_refs: list[dict[str, Any]] | None = None) -> str:
    """One turn of the main agent's conversation. Its Codex home lives in the request folder, so the
    next message resumes the same thread with everything it said, called and saw."""
    ticket = f"job-lead-{pysecrets.token_hex(3)}"
    return run_agent(ws, registry, "job_lead", ticket, prompt, cfg, swarm=swarm, timeout_s=timeout_s, echo=True,
                     codex_home=ws.root / "agent" / "codex", resume=resume, session=session,
                     **({"attachment_refs": attachment_refs} if attachment_refs else {}))


def _compact_rpc(proc, thread_id: str, cfg: dict[str, Any], *, timeout_s: float = 3900) -> None:
    """Drive the native app-server operation; an RPC acknowledgement is not completion.

    A reader thread keeps the deadline effective even when the provider is silent. The caller
    owns the container and always removes it, including on KeyboardInterrupt.
    """
    import queue
    import threading

    incoming: queue.Queue = queue.Queue()

    def read():
        try:
            for line in proc.stdout:
                try:
                    incoming.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            incoming.put(None)

    threading.Thread(target=read, daemon=True, name="vwb-compact-rpc").start()
    deadline = time.monotonic() + timeout_s
    completion = None

    def send(method, params, ident=None):
        row = {"method": method, "params": params}
        if ident is not None:
            row["id"] = ident
        proc.stdin.write(json.dumps(row) + "\n")
        proc.stdin.flush()

    def receive():
        nonlocal completion
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex compaction timed out; the saved conversation is retained")
        try:
            row = incoming.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            return {}
        if row is None:
            raise RuntimeError("Codex app-server exited before compaction completed")
        method, params = row.get("method"), row.get("params") or {}
        if method == "turn/completed" and params.get("threadId") == thread_id:
            completion = params.get("turn") or {}
        if method and "id" in row:  # compaction has no reason to request host actions
            proc.stdin.write(json.dumps({"id": row["id"], "error": {"code": -32601,
                                   "message": "Client actions are unavailable during compaction"}}) + "\n")
            proc.stdin.flush()
        return row

    def request(ident, method, params):
        send(method, params, ident)
        while True:
            row = receive()
            if row.get("id") == ident and "method" not in row:
                if row.get("error"):
                    raise RuntimeError(f"Codex {method}: {row['error'].get('message', row['error'])}")
                return row.get("result") or {}

    request(1, "initialize", {"clientInfo": {"name": "video_workbench", "title": "Video Workbench", "version": "0.1.0"}})
    send("initialized", {})
    request(2, "thread/resume", {"threadId": thread_id, "model": cfg["model"], "modelProvider": "vwb",
                                  "cwd": "/workspace", "approvalPolicy": "never", "sandbox": "danger-full-access"})
    request(3, "thread/compact/start", {"threadId": thread_id})
    while completion is None:
        receive()
    if completion.get("status") != "completed":
        error = completion.get("error") or {}
        raise RuntimeError(f"Codex compaction {completion.get('status', 'failed')}: {error.get('message', '')}")


def compact_thread(ws: Workspace, registry: Registry, thread_id: str, cfg: dict[str, Any]) -> None:
    """Compact an existing Codex thread through its native app-server protocol, in Docker.

    The proxy retains the usual provider logging and credential isolation. No new user turn,
    conversation reset, summarization substitute, or editing action is performed.
    """
    import tempfile

    if not enabled():
        raise HostUnavailable("The Codex container is unavailable. Start Docker and try compaction again.")
    ticket = f"compact-{pysecrets.token_hex(4)}"
    token = pysecrets.token_urlsafe(24)
    proxy = None
    tool_server = None
    proc = None
    try:
        proxy = ModelProxy(CallLog(ws.ledger / "tools.jsonl"), token=token, actor=ticket,
                           service_tier=cfg.get("service_tier"))
        ctx = ToolContext(ws=ws, agent=ticket, role="job_lead", gateway=Gateway(ws, actor=ticket))
        tool_server = ToolServer(ctx, registry, "job_lead", token)
        provider = (f'model_providers.vwb={{name="vwb gateway", base_url="http://host.docker.internal:{proxy.port}/v1", '
                    f'env_key="VWB_TOKEN", wire_api="responses", stream_idle_timeout_ms={STREAM_IDLE_TIMEOUT_MS}}}')
        mcp = (f'mcp_servers.vwb={{url="http://host.docker.internal:{tool_server.port}/mcp", '
               f'bearer_token_env_var="VWB_TOKEN", startup_timeout_sec=60, tool_timeout_sec={TOOL_TIMEOUT_S}}}')
        cmd = ["docker", "run", "--rm", "-i", "--name", f"vwb-{ticket}", "--cpus", "4", "--memory", "4g",
               *_ui_job_labels(),
               "-v", f"{ws.work.resolve()}:/workspace", "-v", f"{(ws.root / 'agent' / 'codex').resolve()}:/home/agent/.codex",
               "-e", f"VWB_TOKEN={token}", IMAGE, "app-server", "--listen", "stdio://",
               "-c", 'model_provider="vwb"', "-c", provider, "-c", mcp,
               "-c", f'model="{cfg["model"]}"', "-c", f'model_reasoning_effort="{cfg.get("effort", "high")}"']
        if cfg.get("context_window"):
            cmd += ["-c", f"model_context_window={int(cfg['context_window'])}"]
        with tempfile.TemporaryFile(mode="w+") as stderr:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True)
            _compact_rpc(proc, thread_id, cfg)
    finally:
        kill(ticket)
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout):
                if pipe:
                    pipe.close()
        if tool_server is not None:
            tool_server.stop()
        if proxy is not None:
            proxy.stop()
