"""Durable serial queues with interrupt-and-resume steering.

Each model turn runs in its own process. Cancellation therefore reaches the existing
SIGINT cleanup paths, and no second turn starts until that process has exited.
Restarting the server never starts a saved job without a new user action.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace
from vwb.workspace.request import now_iso


class QueueConflict(ValueError):
    pass


RESUME_MESSAGE = "Resume the interrupted turn from the saved conversation and workspace."


class TurnQueue:
    def __init__(self, runs_dir: Path, *, command: list[str] | None = None):
        self.runs_dir = runs_dir.resolve()
        self.command = command or [sys.executable, "-m", "vwb.web.worker"]
        self.owns_containers = command is None
        self.lock = threading.RLock()
        self.states: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[str, Workspace] = {}
        self.processes: dict[str, subprocess.Popen] = {}
        self.logs: dict[str, Any] = {}
        self.stopping: dict[str, float] = {}
        self.closed = threading.Event()
        # A second UI server could otherwise launch the same persisted job twice.
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.owner_lock = FileLock(str(self.runs_dir / ".web-server.lock"))
        try:
            self.owner_lock.acquire(timeout=0)
        except Timeout:
            raise QueueConflict("A Video Workbench UI server is already running for this runs folder.") from None
        self.thread = threading.Thread(target=self._dispatch, name="vwb-ui-queue", daemon=True)
        self.thread.start()

    def _path(self, ws: Workspace) -> Path:
        return ws.memory / "web_queue.json"

    def _state(self, ws: Workspace) -> dict[str, Any]:
        known = self.workspaces.get(ws.id)
        if known and known.root.resolve() != ws.root.resolve():
            raise QueueConflict("A different conversation already uses this id.")
        if ws.id not in self.states:
            state = read_json(self._path(ws), {"jobs": [], "paused": False})
            unfinished = False
            recovered = False
            for job in state["jobs"]:
                if job["status"] in {"running", "stopping"}:
                    recovered = True
                    self._cleanup_containers(job["id"])
                    result = read_json(ws.memory / "web_jobs" / f"{job['id']}.result.json", {})
                    # The worker may have finished just before the server lost its
                    # queue write. A durable success receipt must not offer replay.
                    if result.get("status") == "completed":
                        job.update(status="completed", finished_at=now_iso())
                        job.pop("error", None)
                    else:
                        job.update(status="failed" if result.get("status") == "failed" else "stopped",
                                   finished_at=now_iso(), error=result.get("error") or "The UI server stopped during this turn.")
                        unfinished = True
                if job["status"] == "queued":
                    unfinished = True
            if unfinished:
                state["paused"] = True
            if unfinished or recovered:
                write_json(self._path(ws), state)
            self.states[ws.id] = state
            # A registered historical request can live deeper under runs/. Keep
            # its exact location for dispatch, persistence and shutdown.
            self.workspaces[ws.id] = ws
        return self.states[ws.id]

    @staticmethod
    def _external_turn(ws: Workspace) -> bool:
        lock = FileLock(str(ws.root / "turn.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return True
        lock.release()
        return False

    def _project_busy(self, ws: Workspace) -> bool:
        """Serialize editor conversations sharing work/, without claiming their turn.

        The process map covers the gap between spawning a worker and its acquiring
        the project lock. The lock also covers turns launched outside this server.
        This is a dispatch gate only: Stop/Send now still target this conversation.
        """
        project = getattr(ws, "project_root", None)
        if project is None:
            return False
        project = Path(project).resolve()
        if any(rid != ws.id and getattr(self.workspaces[rid], "project_root", None) is not None
               and Path(self.workspaces[rid].project_root).resolve() == project
               for rid in self.processes):
            return True
        lock = FileLock(str(project / "turn.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return True
        lock.release()
        return False

    def snapshot(self, ws: Workspace) -> dict[str, Any]:
        with self.lock:
            state = self._state(ws)
            active = next((j for j in state["jobs"] if j["status"] in {"running", "stopping"}), None)
            if not active and self._external_turn(ws):
                active = {"id": None, "kind": "external", "status": "running", "can_stop": False}
            done = [j for j in state["jobs"] if j["status"] not in {"queued", "running", "stopping"}]
            return {"active": dict(active) if active else None,
                    "queue": [dict(j) for j in state["jobs"] if j["status"] == "queued"],
                    "paused": state["paused"], "last_job": dict(done[-1]) if done else None,
                    "resumable_job": dict(job) if not active and (job := self._resumable(state)) else None}

    @staticmethod
    def _resumable(state: dict[str, Any]) -> dict[str, Any] | None:
        finished = [j for j in state["jobs"] if j["status"] in {"completed", "failed", "stopped"}
                    or j["status"] == "cancelled" and j.get("resume_of")]
        if not finished:
            return None
        job = max(enumerate(finished), key=lambda item: (item[1].get("finished_at") or "", item[0]))[1]
        if job["status"] in {"failed", "stopped", "cancelled"} and job["kind"] in {"message", "compact"} and not job.get("resumed_by"):
            return job
        return None

    def enqueue(self, ws: Workspace, *, message: str = "", mode: str = "queue", kind: str = "message",
                attachments: list[str] | None = None, attachment_refs: list[dict[str, Any]] | None = None,
                skip_index: bool = True,
                context: dict[str, list[str]] | None = None, main_config: dict[str, Any] | None = None,
                idempotency_key: str | None = None, turn_lock_held: bool = False) -> dict[str, Any]:
        from vwb.workspace.references import validate
        context = validate(context, ws)
        from vwb.workspace.attachments import validate_refs
        attachment_refs = validate_refs(ws, attachment_refs)
        if mode not in {"queue", "now"} or kind not in {"message", "compact"}:
            raise ValueError("Unknown message mode or operation.")
        if kind == "message" and (not isinstance(message, str) or (not message.strip() and not attachment_refs and not attachments)):
            raise ValueError("Write a message first.")
        if len(message) > 100_000:
            raise ValueError("The message is too long (maximum 100,000 characters).")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 240):
            raise ValueError("Invalid submission key.")
        from vwb.util.hashing import sha256_text
        from vwb.util.jsonio import canonical_json
        signature = sha256_text(canonical_json({"message": message.strip(), "mode": mode, "kind": kind,
                                                "attachments": attachments or [], "attachment_refs": attachment_refs, "skip_index": skip_index,
                                                "context": context}))
        with self.lock:
            if self.closed.is_set():
                raise QueueConflict("The UI server is shutting down.")
            state = self._state(ws)
            if idempotency_key is not None:
                previous = next((j for j in state["jobs"] if j.get("idempotency_key") == idempotency_key), None)
                if previous:
                    if previous.get("submission_digest") != signature:
                        raise QueueConflict("This submission key already belongs to a different message.")
                    return dict(previous)
            if mode == "now" and not turn_lock_held and ws.id not in self.processes and self._external_turn(ws):
                raise QueueConflict("This turn was started outside the UI. Stop it in its terminal, or queue your message.")
            job = {"id": uuid.uuid4().hex, "kind": kind, "message": message.strip(), "status": "queued",
                   "created_at": now_iso(), "attachments": attachments or [], "attachment_refs": attachment_refs, "skip_index": skip_index,
                   "context": context,
                   "can_stop": True}
            if idempotency_key is not None:
                job.update(idempotency_key=idempotency_key, submission_digest=signature)
            if kind == "message":
                from vwb.config import slot
                from vwb.prompts import with_snapshot
                if getattr(ws, "conversation_lane", "edit") == "qc":
                    from copy import deepcopy
                    if main_config is None:
                        raise ValueError("QC jobs require a captured QC configuration.")
                    job["main_config"] = deepcopy(main_config)
                else:
                    job["main_config"] = with_snapshot(main_config if main_config is not None else slot("main"), ws, self.runs_dir)
            elif main_config is not None:
                from copy import deepcopy
                job["main_config"] = deepcopy(main_config)
            if mode == "now":
                insert = next((i for i, j in enumerate(state["jobs"]) if j["status"] == "queued"), len(state["jobs"]))
                state["jobs"].insert(insert, job)
                state["paused"] = False
                self._interrupt(ws)
            else:
                state["jobs"].append(job)
            self._save(ws)
            return dict(job)

    def recheck_job(self, ws: Workspace, recheck_id: str) -> dict[str, Any] | None:
        """Latest attempt, including explicit resumes, without provider settings."""
        with self.lock:
            for job in reversed(self._state(ws)["jobs"]):
                if job.get("main_config", {}).get("qc_recheck_id") == recheck_id:
                    return {key: job[key] for key in ("id", "status", "error", "created_at", "started_at", "finished_at")
                            if key in job}
        return None

    def cancel(self, ws: Workspace, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = next((j for j in self._state(ws)["jobs"] if j["id"] == job_id), None)
            if not job or job["status"] != "queued":
                raise QueueConflict("Only a queued message can be removed.")
            job.update(status="cancelled", finished_at=now_iso())
            self._save(ws)
            return dict(job)

    def stop(self, ws: Workspace) -> dict[str, Any]:
        with self.lock:
            if ws.id not in self.processes and self._external_turn(ws):
                raise QueueConflict("This turn was started outside the UI. Stop it in its terminal.")
            self._state(ws)["paused"] = True
            self._interrupt(ws)
            self._save(ws)
            return self.snapshot(ws)

    def submitted(self, ws: Workspace, idempotency_key: str) -> dict | None:
        """Recover an accepted submission even while it is active or paused."""
        with self.lock:
            job = next((job for job in self._state(ws)["jobs"] if job.get("idempotency_key") == idempotency_key), None)
            return dict(job) if job else None

    def reset(self, ws: Workspace, *, turn_lock_held: bool = False) -> None:
        """Forget queued jobs and resume links after an explicit history rewind.

        Rewind callers must hold their mutation/turn locks across the history edit
        and this reset. Pass turn_lock_held=True only when holding this workspace's
        turn.lock; otherwise this method checks for a terminal-owned turn itself.
        Worker receipts remain on disk for diagnostics, but cannot launch work.
        """
        with self.lock:
            if self.closed.is_set():
                raise QueueConflict("The UI server is shutting down.")
            state = self._state(ws)
            if (ws.id in self.processes
                    or any(j["status"] in {"running", "stopping"} for j in state["jobs"])
                    or not turn_lock_held and self._external_turn(ws)):
                raise QueueConflict("Stop the active turn before resetting its queue.")
            state.update(jobs=[], paused=True)
            self._save(ws)

    def resume(self, ws: Workspace, *, job_id: str | None = None,
               main_config: dict[str, Any] | None = None,
               prompt: dict[str, str] | None = None) -> dict[str, Any]:
        with self.lock:
            if self.closed.is_set():
                raise QueueConflict("The UI server is shutting down.")
            if (main_config is not None or prompt is not None) and job_id is None:
                raise ValueError("Choose an interrupted turn before changing its continuation model.")
            state = self._state(ws)
            if job_id is not None:
                if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
                    raise ValueError("Choose a valid interrupted turn to resume.")
                source = next((j for j in state["jobs"] if j["id"] == job_id), None)
                if source and source.get("resumed_by"):
                    # A retried click must not enqueue again or unpause a subsequently stopped queue.
                    return self.snapshot(ws)
                if (ws.id in self.processes or self._external_turn(ws)
                        or any(j["status"] in {"running", "stopping"} for j in state["jobs"])):
                    raise QueueConflict("Wait for the active turn to finish before resuming an interrupted turn.")
                if source is None or source is not self._resumable(state):
                    raise QueueConflict("That turn can no longer be resumed. Refresh the conversation.")
                from copy import deepcopy
                job = {"id": uuid.uuid4().hex, "kind": source["kind"],
                       "message": RESUME_MESSAGE if source["kind"] == "message" else "",
                       "status": "queued", "created_at": now_iso(), "can_stop": True,
                       "resume_of": source["id"], "skip_index": source.get("skip_index", True),
                       "attachments": deepcopy(source.get("attachments", [])),
                       "attachment_refs": deepcopy(source.get("attachment_refs", [])),
                       "context": deepcopy(source.get("context", {}))}
                if source["kind"] == "message":
                    job["resume_message"] = source.get("resume_message", source.get("message", ""))
                if main_config is not None:
                    job["main_config"] = deepcopy(main_config)
                elif "main_config" in source:
                    job["main_config"] = deepcopy(source["main_config"])
                if prompt is not None:
                    from vwb.config import slot
                    from vwb.prompts import validate_snapshot
                    job.setdefault("main_config", slot("main"))["prompt"] = validate_snapshot(prompt)
                insert = next((i for i, j in enumerate(state["jobs"]) if j["status"] == "queued"), len(state["jobs"]))
                state["jobs"].insert(insert, job)
                source["resumed_by"] = job["id"]
            state["paused"] = False
            self._save(ws)
            return self.snapshot(ws)

    def _save(self, ws: Workspace) -> None:
        write_json(self._path(ws), self.states[ws.id])

    def _cleanup_containers(self, job_id: str) -> None:
        """Remove only containers labelled for this UI-owned job after an abnormal exit."""
        if not self.owns_containers or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            return
        try:
            result = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=vwb.ui-job={job_id}"],
                                    capture_output=True, text=True, timeout=3)
            ids = [value for value in result.stdout.split() if re.fullmatch(r"[a-f0-9]{12,64}", value)]
            if ids:
                subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass  # Docker can be unavailable; the normal host and parent watchdog also clean up.

    def _interrupt(self, ws: Workspace) -> None:
        proc = self.processes.get(ws.id)
        if not proc or proc.poll() is not None or ws.id in self.stopping:
            return
        self.stopping[ws.id] = time.monotonic()
        for job in self._state(ws)["jobs"]:
            if job["status"] == "running":
                job["status"] = "stopping"
        self._signal_group(proc, signal.SIGINT)

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            # On macOS a group whose final process just exited can produce EPERM.
            # Reap first; Popen.send_signal also guards against a recycled PID.
            if proc.poll() is None:
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    pass

    def _start(self, ws: Workspace, job: dict[str, Any]) -> None:
        directory = ws.memory / "web_jobs"
        job_path = directory / f"{job['id']}.json"
        lane = getattr(ws, "conversation_lane", "edit")
        metadata = {"lane": lane, "parent_request": str(ws.parent.root)} if lane == "qc" else {}
        write_json(job_path, {**job, **metadata, "request": str(ws.root), "parent_pid": os.getpid()})
        log = (directory / f"{job['id']}.log").open("ab")
        env = {**os.environ, "VWB_RUNS_DIR": str(self.runs_dir), "PYTHONUNBUFFERED": "1", "VWB_UI_JOB_ID": job["id"]}
        try:
            proc = subprocess.Popen([*self.command, "--job", str(job_path)], stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        except Exception:
            log.close()
            raise
        self.processes[ws.id], self.logs[ws.id] = proc, log
        job.update(status="running", started_at=now_iso())
        self._save(ws)

    def _tick(self) -> None:
        with self.lock:
            for rid, state in self.states.items():
                ws = self.workspaces[rid]
                proc = self.processes.get(rid)
                if proc and proc.poll() is not None:
                    job = next(j for j in state["jobs"] if j["status"] in {"running", "stopping"})
                    result = read_json(ws.memory / "web_jobs" / f"{job['id']}.result.json", {})
                    stopped = rid in self.stopping
                    if stopped or proc.returncode != 0:
                        self._cleanup_containers(job["id"])
                    status = "completed" if result.get("status") == "completed" else (
                        "stopped" if stopped else ("completed" if proc.returncode == 0 else "failed"))
                    job.update(status=status,
                               finished_at=now_iso())
                    if status == "completed":
                        job.pop("error", None)
                    elif result.get("error"):
                        job["error"] = result["error"]
                    elif job["status"] == "failed":
                        job["error"] = "The turn exited unexpectedly. Send a message to resume the conversation."
                    if job["status"] == "failed":
                        state["paused"] = True
                    self.logs.pop(rid).close()
                    self.processes.pop(rid)
                    self.stopping.pop(rid, None)
                    self._save(ws)
                    proc = None
                if proc and rid in self.stopping:
                    elapsed = time.monotonic() - self.stopping[rid]
                    if elapsed > 30:
                        self._signal_group(proc, signal.SIGKILL if elapsed > 35 else signal.SIGTERM)
                if not proc and not state["paused"] and not self.closed.is_set():
                    job = next((j for j in state["jobs"] if j["status"] == "queued"), None)
                    if job and not self._external_turn(ws) and not self._project_busy(ws):
                        try:
                            self._start(ws, job)
                        except Exception as exc:
                            job.update(status="failed", error=str(exc), finished_at=now_iso())
                            state["paused"] = True
                            self._save(ws)

    def _dispatch(self) -> None:
        while not self.closed.wait(0.2):
            self._tick()

    def close(self) -> None:
        self.closed.set()
        self.thread.join(timeout=2)
        try:
            with self.lock:
                for rid in list(self.processes):
                    ws = self.workspaces[rid]
                    self.states[rid]["paused"] = True
                    self._interrupt(ws)
                    self._save(ws)
            deadline = time.monotonic() + 37
            while self.processes and time.monotonic() < deadline:
                self._tick()
                time.sleep(0.05)
        finally:
            self.owner_lock.release()
