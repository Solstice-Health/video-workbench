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


class TurnQueue:
    def __init__(self, runs_dir: Path, *, command: list[str] | None = None):
        self.runs_dir = runs_dir.resolve()
        self.command = command or [sys.executable, "-m", "vwb.web.worker"]
        self.owns_containers = command is None
        self.lock = threading.RLock()
        self.states: dict[str, dict[str, Any]] = {}
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
        if ws.id not in self.states:
            state = read_json(self._path(ws), {"jobs": [], "paused": False})
            unfinished = False
            for job in state["jobs"]:
                if job["status"] in {"running", "stopping"}:
                    self._cleanup_containers(job["id"])
                    job.update(status="stopped", finished_at=now_iso(), error="The UI server stopped during this turn.")
                    unfinished = True
                if job["status"] == "queued":
                    unfinished = True
            if unfinished:
                state["paused"] = True
                write_json(self._path(ws), state)
            self.states[ws.id] = state
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

    def snapshot(self, ws: Workspace) -> dict[str, Any]:
        with self.lock:
            state = self._state(ws)
            active = next((j for j in state["jobs"] if j["status"] in {"running", "stopping"}), None)
            if not active and self._external_turn(ws):
                active = {"id": None, "kind": "external", "status": "running", "can_stop": False}
            done = [j for j in state["jobs"] if j["status"] not in {"queued", "running", "stopping"}]
            return {"active": dict(active) if active else None,
                    "queue": [dict(j) for j in state["jobs"] if j["status"] == "queued"],
                    "paused": state["paused"], "last_job": dict(done[-1]) if done else None}

    def enqueue(self, ws: Workspace, *, message: str = "", mode: str = "queue", kind: str = "message",
                attachments: list[str] | None = None, skip_index: bool = True,
                context: dict[str, list[str]] | None = None, main_config: dict[str, Any] | None = None) -> dict[str, Any]:
        from vwb.workspace.references import validate
        context = validate(context, ws)
        if mode not in {"queue", "now"} or kind not in {"message", "compact"}:
            raise ValueError("Unknown message mode or operation.")
        if kind == "message" and (not isinstance(message, str) or not message.strip()):
            raise ValueError("Write a message first.")
        if len(message) > 100_000:
            raise ValueError("The message is too long (maximum 100,000 characters).")
        with self.lock:
            if self.closed.is_set():
                raise QueueConflict("The UI server is shutting down.")
            state = self._state(ws)
            if mode == "now" and ws.id not in self.processes and self._external_turn(ws):
                raise QueueConflict("This turn was started outside the UI. Stop it in its terminal, or queue your message.")
            job = {"id": uuid.uuid4().hex, "kind": kind, "message": message.strip(), "status": "queued",
                   "created_at": now_iso(), "attachments": attachments or [], "skip_index": skip_index,
                   "context": context,
                   "can_stop": True}
            if main_config is not None:
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

    def resume(self, ws: Workspace) -> dict[str, Any]:
        with self.lock:
            self._state(ws)["paused"] = False
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
        write_json(job_path, {**job, "request": str(ws.root), "parent_pid": os.getpid()})
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
                ws = Workspace(self.runs_dir / rid)
                proc = self.processes.get(rid)
                if proc and proc.poll() is not None:
                    job = next(j for j in state["jobs"] if j["status"] in {"running", "stopping"})
                    result = read_json(ws.memory / "web_jobs" / f"{job['id']}.result.json", {})
                    stopped = rid in self.stopping
                    if stopped or proc.returncode != 0:
                        self._cleanup_containers(job["id"])
                    job.update(status="stopped" if stopped else ("completed" if proc.returncode == 0 else "failed"),
                               finished_at=now_iso())
                    if result.get("error"):
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
                    if job and not self._external_turn(ws):
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
                    ws = Workspace(self.runs_dir / rid)
                    self.states[rid]["paused"] = True
                    self._interrupt(ws)
                    self._save(ws)
            deadline = time.monotonic() + 37
            while self.processes and time.monotonic() < deadline:
                self._tick()
                time.sleep(0.05)
        finally:
            self.owner_lock.release()
