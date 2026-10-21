"""Explicit continuation after interrupted turns, without models or subprocesses."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from filelock import FileLock

from vwb.util.jsonio import read_json, write_json
from vwb.web.queue import QueueConflict, TurnQueue
from vwb.workspace import Workspace, attachments
from vwb.workspace.request import SUBDIRS, Request

SOURCE = "a" * 32
PENDING = "b" * 32
LATER = "c" * 32
NEWER = "d" * 32


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "r-resume")
    for sub in SUBDIRS:
        (workspace.root / sub).mkdir(parents=True, exist_ok=True)
    workspace.save(Request(id=workspace.id, message="Make ten short videos"))
    return workspace


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    # State transitions are exercised synchronously; no worker or Docker starts.
    monkeypatch.setattr(TurnQueue, "_dispatch", lambda self: None)


def saved_job(job_id=SOURCE, *, status="stopped", kind="message", **changes):
    return {
        "id": job_id, "kind": kind, "status": status,
        "message": "Make ten short videos, thirty seconds each.",
        "created_at": "2026-10-19T10:00:00+00:00", "can_stop": True,
        "attachments": [], "attachment_refs": [], "skip_index": True,
        "context": {"skills": [], "folders": []}, **changes,
    }


def seed(ws, jobs, *, paused=True):
    write_json(ws.memory / "web_queue.json", {"jobs": jobs, "paused": paused})


def jobs(ws):
    return read_json(ws.memory / "web_queue.json")["jobs"]


@pytest.mark.parametrize("status,kind,eligible", [
    ("stopped", "message", True), ("failed", "message", True),
    ("stopped", "compact", True), ("failed", "compact", True),
    ("completed", "message", False), ("cancelled", "message", False),
])
def test_snapshot_offers_only_interrupted_or_failed_work(ws, tmp_path, status, kind, eligible):
    seed(ws, [saved_job(status=status, kind=kind)])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        candidate = queue.snapshot(ws)["resumable_job"]
        assert (candidate is not None) is eligible
        if eligible:
            assert candidate["id"] == SOURCE and candidate["kind"] == kind
    finally:
        queue.close()


def test_resume_preserves_brief_and_frozen_inputs_before_pending_messages(ws, tmp_path):
    source_file = tmp_path / "brief.txt"
    source_file.write_text("Original supporting notes")
    refs = attachments.ingest(ws, [source_file])
    context = {"skills": [], "folders": ["."], "plugins": ["figma_direct"]}
    config = {"provider": "openai", "model": "saved-model", "reasoning": {"effort": "high"}}
    original = saved_job(attachment_refs=refs, context=context, main_config=config, skip_index=False)
    pending = saved_job(LATER, status="queued", message="Then change the title")
    seed(ws, [original, pending])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        result = queue.resume(ws, job_id=original["id"])
        assert not result["paused"]
        assert result["queue"][1]["id"] == pending["id"]
        continuation = result["queue"][0]
        assert continuation["resume_of"] == original["id"]
        assert continuation["resume_message"] == original["message"]
        assert continuation["message"] and continuation["message"] != original["message"]
        for field in ("context", "main_config", "attachment_refs", "skip_index"):
            assert continuation[field] == original[field]
        assert jobs(ws)[0]["resumed_by"] == continuation["id"]
        # A settings or metadata edit to the old job cannot mutate the new turn.
        state = queue._state(ws)
        source = next(job for job in state["jobs"] if job["id"] == original["id"])
        source["main_config"]["reasoning"]["effort"] = "low"
        source["context"]["plugins"].clear()
        source["attachment_refs"][0]["name"] = "changed.txt"
        resumed = next(job for job in state["jobs"] if job["id"] == continuation["id"])
        assert resumed["main_config"] == config
        assert resumed["context"] == context
        assert resumed["attachment_refs"] == refs
    finally:
        queue.close()


def test_resume_compaction_retries_compaction_instead_of_sending_a_chat_message(ws, tmp_path):
    seed(ws, [saved_job(kind="compact", message="")])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        result = queue.resume(ws, job_id=SOURCE)
        assert len(result["queue"]) == 1
        assert result["queue"][0]["kind"] == "compact"
        assert result["queue"][0]["resume_of"] == SOURCE
        assert not ws.load().chat
    finally:
        queue.close()


def test_explicit_resume_model_override_preserves_original_execution_record(ws, tmp_path):
    original = saved_job(main_config={"model": "gpt-6-astra", "provider": "openai", "effort": "max"})
    seed(ws, [original])
    override = {"model": "claude-opus-5-5", "provider": "anthropic", "host": "anthropic_api", "effort": "max"}
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        continuation = queue.resume(ws, job_id=SOURCE, main_config=override)["queue"][0]
        assert continuation["main_config"] == override
        assert continuation["resume_of"] == SOURCE
        assert continuation["resume_message"] == original["message"]
        assert jobs(ws)[0]["main_config"] == original["main_config"]
        override["effort"] = "low"
        assert queue.snapshot(ws)["queue"][0]["main_config"]["effort"] == "max"
    finally:
        queue.close()


def test_resume_cannot_silently_change_already_queued_models(ws, tmp_path):
    seed(ws, [saved_job(PENDING, status="queued")])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        with pytest.raises(ValueError, match="interrupted turn"):
            queue.resume(ws, main_config={"model": "claude-opus-5-5"})
        assert queue.snapshot(ws)["paused"]
    finally:
        queue.close()


def test_duplicate_resume_stays_idempotent_after_cancellation_and_server_restart(ws, tmp_path):
    seed(ws, [saved_job()])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        queue.resume(ws, job_id=SOURCE)
        continuation = next(job for job in jobs(ws) if job.get("resume_of") == SOURCE)
        queue.resume(ws, job_id=SOURCE)
        assert len(jobs(ws)) == 2
        queue.cancel(ws, continuation["id"])
        queue.resume(ws, job_id=SOURCE)
        assert len(jobs(ws)) == 2 and jobs(ws)[1]["status"] == "cancelled"
    finally:
        queue.close()
    restarted = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        restarted.resume(ws, job_id=SOURCE)
        assert len(jobs(ws)) == 2
        assert jobs(ws)[1]["id"] == continuation["id"]
        assert jobs(ws)[1]["status"] == "cancelled"
        assert restarted.snapshot(ws)["resumable_job"]["id"] == continuation["id"]
    finally:
        restarted.close()


def test_cancelled_continuation_can_be_resumed_by_its_new_id_without_replaying_old_click(ws, tmp_path):
    original = saved_job()
    seed(ws, [original])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        first = queue.resume(ws, job_id=SOURCE)["queue"][0]
        queue.cancel(ws, first["id"])
        assert queue.snapshot(ws)["resumable_job"]["id"] == first["id"]
        queue.resume(ws, job_id=SOURCE)
        assert len(jobs(ws)) == 2 and not queue.snapshot(ws)["queue"]
        resumed = queue.resume(ws, job_id=first["id"])["queue"][0]
        assert resumed["resume_of"] == first["id"]
        assert resumed["resume_message"] == original["message"]
        assert resumed["message"] != original["message"]
        assert jobs(ws)[0]["resumed_by"] == first["id"]
        assert jobs(ws)[1]["resumed_by"] == resumed["id"]
        queue.resume(ws, job_id=SOURCE)
        queue.resume(ws, job_id=first["id"])
        assert len(jobs(ws)) == 3
        assert [job["id"] for job in queue.snapshot(ws)["queue"]] == [resumed["id"]]
    finally:
        queue.close()


@pytest.mark.parametrize("source_status,later", [
    ("completed", None), ("cancelled", None), ("queued", None),
    ("stopped", saved_job(NEWER, status="completed")),
    ("failed", saved_job(NEWER, status="stopped")),
])
def test_resume_rejects_stale_or_ineligible_job_without_modifying_queue(ws, tmp_path, source_status, later):
    seed(ws, [saved_job(status=source_status)] + ([later] if later else []))
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        queue.snapshot(ws)
        before = deepcopy(jobs(ws))
        with pytest.raises(QueueConflict):
            queue.resume(ws, job_id=SOURCE)
        with pytest.raises(QueueConflict):
            queue.resume(ws, job_id="f" * 32)
        assert jobs(ws) == before
    finally:
        queue.close()


@pytest.mark.parametrize("active", ["ui", "external"])
def test_resume_rejects_an_active_turn_and_hides_resume_action(ws, tmp_path, active):
    seed(ws, [saved_job()])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    external = FileLock(str(ws.root / "turn.lock"))
    try:
        state = queue._state(ws)
        if active == "ui":
            state["jobs"].append(saved_job("running", status="running"))
            queue._save(ws)
        else:
            external.acquire(timeout=0)
        before = deepcopy(jobs(ws))
        assert queue.snapshot(ws)["active"] is not None
        assert queue.snapshot(ws)["resumable_job"] is None
        with pytest.raises(QueueConflict):
            queue.resume(ws, job_id=SOURCE)
        assert jobs(ws) == before
    finally:
        external.release()
        queue.close()


def test_restart_stops_orphaned_turn_and_never_replays_paid_work(ws, tmp_path, monkeypatch):
    seed(ws, [saved_job(status="running"), saved_job(PENDING, status="queued")], paused=False)
    starts = []
    monkeypatch.setattr(TurnQueue, "_start", lambda self, workspace, job: starts.append(job["id"]))
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        state = queue.snapshot(ws)
        assert state["paused"] and state["active"] is None
        assert state["resumable_job"]["id"] == SOURCE
        assert jobs(ws)[0]["status"] == "stopped"
        assert state["queue"][0]["id"] == PENDING
        queue._tick()
        assert not starts
        assert len(jobs(ws)) == 2
    finally:
        queue.close()


@pytest.mark.parametrize("receipt_status", ["completed", "failed"])
def test_restart_recovers_durable_worker_receipt_and_persists_final_state(ws, tmp_path, receipt_status):
    seed(ws, [saved_job(status="running", error="Stale error")], paused=False)
    receipt = {"status": receipt_status}
    if receipt_status == "failed":
        receipt["error"] = "The connection was interrupted"
    write_json(ws.memory / "web_jobs" / f"{SOURCE}.result.json", receipt)
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        result = queue.snapshot(ws)
        assert result["active"] is None
        assert jobs(ws)[0]["status"] == receipt_status
        if receipt_status == "completed":
            assert result["resumable_job"] is None
            assert "error" not in jobs(ws)[0]
            with pytest.raises(QueueConflict):
                queue.resume(ws, job_id=SOURCE)
        else:
            assert result["resumable_job"]["id"] == SOURCE
            assert result["paused"]
            assert jobs(ws)[0]["error"] == receipt["error"]
    finally:
        queue.close()


@pytest.mark.parametrize("stopping,exit_code", [(True, 130), (False, 1), (True, 0)])
def test_completion_receipt_wins_over_late_stop_or_nonzero_worker_exit(ws, tmp_path, stopping, exit_code):
    seed(ws, [saved_job()])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        state = queue._state(ws)
        state["jobs"][0].update(status="stopping" if stopping else "running", error="Stale error")
        process = Mock(returncode=exit_code)
        process.poll.return_value = exit_code
        queue.processes[ws.id] = process
        log = Mock()
        queue.logs[ws.id] = log
        if stopping:
            queue.stopping[ws.id] = 0
        write_json(ws.memory / "web_jobs" / f"{SOURCE}.result.json", {"status": "completed"})
        queue._tick()
        assert jobs(ws)[0]["status"] == "completed"
        assert "error" not in jobs(ws)[0]
        assert queue.snapshot(ws)["resumable_job"] is None
        assert queue.snapshot(ws)["active"] is None
        assert ws.id not in queue.processes and ws.id not in queue.stopping
        log.close.assert_called_once()
    finally:
        queue.close()


def test_legacy_resume_only_unpauses_pending_queue(ws, tmp_path):
    seed(ws, [saved_job(), saved_job(PENDING, status="queued")])
    queue = TurnQueue(tmp_path, command=["unused-worker"])
    try:
        result = queue.resume(ws)
        assert not result["paused"]
        assert [job["id"] for job in result["queue"]] == [PENDING]
        assert len(jobs(ws)) == 2
        assert all("resume_of" not in job and "resumed_by" not in job for job in jobs(ws))
    finally:
        queue.close()


@pytest.mark.parametrize("resuming", [False, True])
def test_worker_forwards_private_original_brief_only_for_a_resume_job(ws, tmp_path, monkeypatch, resuming):
    from vwb import orchestrator
    from vwb.web import worker

    job = saved_job("continuation", status="queued", message="Resume the interrupted turn.", request=str(ws.root))
    if resuming:
        job["resume_message"] = "The original brief"
        job["resume_of"] = SOURCE
    path = tmp_path / "job.json"
    write_json(path, job)
    calls = []
    monkeypatch.setattr("sys.argv", ["worker", "--job", str(path)])
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(orchestrator, "chat", lambda *args, **kwargs: calls.append((args, kwargs)) or {"reply": "Done"})
    assert worker.main() == 0
    assert calls[0][0] == (job["message"],)
    assert calls[0][1].get("resume_message") == (job["resume_message"] if resuming else None)
    assert read_json(path.with_suffix(".result.json"))["status"] == "completed"


def test_resume_prompt_restores_brief_without_duplicating_it_in_public_chat(ws, monkeypatch):
    from vwb import orchestrator

    original = "Create ten thirty-second clips using the approved assets."
    ws.add_chat("human", original)
    ws.add_chat("job_lead", "I finished three clips before interruption.")
    seen = []
    monkeypatch.setattr(orchestrator.job_lead, "run", lambda workspace, message, **kwargs: seen.append(message) or "Done")
    monkeypatch.setattr(orchestrator, "ensure_review", lambda workspace: [])
    monkeypatch.setattr(orchestrator.page, "build", lambda workspace: Path("review.html"))
    action = "Resume the interrupted turn."
    orchestrator.chat(action, request_id=str(ws.root), skip_index=True, resume_message=original)
    assert len(seen) == 1 and original in seen[0] and action in seen[0]
    assert any(word in seen[0].lower() for word in ("side effect", "side-effect", "already completed", "repeat"))
    public = [turn.text for turn in ws.load().chat if turn.role == "human"]
    assert public == [original, action]
