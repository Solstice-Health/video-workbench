"""Human answers and instructions keep distinct delivery and permission semantics."""

import json
import sys
import threading
import time
from http.client import HTTPConnection

import pytest
from filelock import FileLock

from vwb.ask.broker import Broker
from vwb.util.jsonio import read_jsonl
from vwb.web.queue import QueueConflict, TurnQueue
from vwb.web.server import Workbench, WorkbenchServer
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


def workspace(root, rid):
    ws = Workspace(root / rid)
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True)
    ws.save(Request(id=rid, message="Review choices"))
    return ws


def until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(.01)
    pytest.fail("delivery did not complete")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    script = tmp_path / "worker.py"
    script.write_text('''import json, pathlib, sys, time
job = json.loads(pathlib.Path(sys.argv[2]).read_text())
log = pathlib.Path(job['request']) / 'executions.jsonl'
def record(event):
    with log.open('a') as out:
        out.write(json.dumps({'event': event, 'message': job['message']}) + '\\n')
try:
    record('start')
    time.sleep(30 if job['message'] == 'hold' else .05)
    record('finish')
except KeyboardInterrupt:
    time.sleep(.05)
    record('stopped')
    sys.exit(130)
''')
    ws = workspace(tmp_path, "r-one")
    queue = TurnQueue(tmp_path, command=[sys.executable, str(script)])
    app = Workbench(tmp_path, queue=queue)
    monkeypatch.setattr("vwb.workspace.ledger.control_key", lambda: b"test-key")
    monkeypatch.setattr("vwb.web.server.models.explicit_runtime", lambda *a: None)
    try:
        yield app, ws
    finally:
        app.local_imports.close()
        queue.close()


def question(ws, **kwargs):
    return Broker(ws).ask(asker="job_lead", question="Allow this change?", why="Review",
                          options=[{"id": "grant", "label": "Allow"}, {"id": "deny", "label": "Deny"}],
                          recommended="deny", **kwargs)


@pytest.mark.parametrize("ask_mode", ["non_blocking", "blocking"])
@pytest.mark.parametrize("mode", ["now", "queue"])
def test_answer_without_live_waiter_respects_explicit_delivery(harness, ask_mode, mode):
    app, ws = harness
    queue = app.queue
    queue.enqueue(ws, message="hold")
    until(lambda: read_jsonl(ws.root / "executions.jsonl"))
    queued = queue.enqueue(ws, message="later instruction")
    ticket = question(ws, mode=ask_mode)
    result = app.answer(ws, {"ticket": ticket["id"], "option": "deny", "text": "Keep the wide shot", "mode": mode})
    assert result["ticket"]["answer"]["option"] == "deny"
    if mode == "queue":
        assert result["delivery"] == "queued"
        assert [j["id"] for j in queue.snapshot(ws)["queue"]] == [queued["id"], result["job"]["id"]]
        assert ws.id not in queue.stopping
        queue.stop(ws)
    else:
        assert result["delivery"] == "interrupt_then_resume"
        until(lambda: len(read_jsonl(ws.root / "executions.jsonl")) == 6)
        events = read_jsonl(ws.root / "executions.jsonl")
        assert [e["event"] for e in events] == ["start", "stopped", "start", "finish", "start", "finish"]
        assert events[2]["message"] == "Answer to: Allow this change?\nDeny\nKeep the wide shot"
        assert events[4]["message"] == "later instruction"
    with pytest.raises(QueueConflict, match="already been answered"):
        app.answer(ws, {"ticket": ticket["id"], "option": "deny", "mode": mode})
    assert len([e for e in read_jsonl(ws.ledger / "ledger.jsonl") if e["kind"] == "human.answer"]) == 1


@pytest.mark.parametrize("mode", ["now", "queue"])
def test_external_turn_answer_is_queued_or_rejected_without_signing(harness, mode):
    app, ws = harness
    ticket = question(ws)
    with FileLock(str(ws.root / "turn.lock")):
        if mode == "now":
            with pytest.raises(QueueConflict, match="outside the UI"):
                app.answer(ws, {"ticket": ticket["id"], "option": "grant", "mode": mode})
            assert Broker(ws).tickets()[0]["status"] == "open"
            assert not app.queue.snapshot(ws)["queue"]
        else:
            response = app.answer(ws, {"ticket": ticket["id"], "option": "grant", "mode": mode})
            assert response["delivery"] == "queued"


@pytest.mark.parametrize("mode", ["now", "queue"])
def test_general_instruction_keeps_attachment_and_cannot_answer_permission(harness, mode):
    app, ws = harness
    ticket = question(ws, kind="allow", mode="blocking")
    app.queue.enqueue(ws, message="hold")
    until(lambda: read_jsonl(ws.root / "executions.jsonl"))
    server = WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, body, **headers):
        conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("POST", path, body, {"X-CSRF-Token": app.csrf_token, **headers})
        result = conn.getresponse()
        status, data = result.status, json.loads(result.read())
        conn.close()
        assert status in {201, 202}, data
        return data

    try:
        upload = post("/api/uploads", b"new instruction attachment", **{"X-Filename": "notes.txt"})["upload"]
        result = post(f"/api/requests/{ws.id}/messages", json.dumps({
            "message": "grant", "ticket": ticket["id"], "option": "grant", "mode": mode,
            "attachments": [upload["id"]],
        }), **{"Content-Type": "application/json"})
        ref, = result["job"]["attachment_refs"]
        assert (ws.work / ref["path"]).read_bytes() == b"new instruction attachment"
        assert Broker(ws).tickets()[0]["status"] == "open"
        assert not [e for e in read_jsonl(ws.ledger / "ledger.jsonl") if e["kind"] == "human.answer"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_project_dispatch_serializes_siblings_before_child_lock_and_preserves_other_projects(harness, tmp_path, monkeypatch):
    app, first = harness
    project = tmp_path / "project"
    project.mkdir()
    sibling = workspace(tmp_path, "r-sibling")
    unrelated = workspace(tmp_path, "r-unrelated")
    monkeypatch.setattr(Workspace, "project_root", property(lambda ws: project if ws.id in {first.id, sibling.id} else None))
    with app.queue.lock:
        app.queue.enqueue(first, message="hold")
        app.queue.enqueue(sibling, message="older instruction")
        app.queue.enqueue(unrelated, message="hold")
        app.queue._tick()
        assert set(app.queue.processes) == {first.id, unrelated.id}
        assert app.queue.snapshot(sibling)["active"] is None
        assert len(app.queue.snapshot(sibling)["queue"]) == 1
        # Prioritize this conversation, without stopping its project's active one.
        app.queue.enqueue(sibling, message="priority instruction", mode="now")
        app.queue._tick()
        assert first.id not in app.queue.stopping
        assert set(app.queue.processes) == {first.id, unrelated.id}
    until(lambda: read_jsonl(first.root / "executions.jsonl"))
    app.queue.stop(first)
    until(lambda: len(read_jsonl(sibling.root / "executions.jsonl")) == 4)
    assert [e["message"] for e in read_jsonl(sibling.root / "executions.jsonl")][::2] == [
        "priority instruction", "older instruction"]


def test_project_lock_blocks_dispatch_without_claiming_current_conversation(harness, tmp_path, monkeypatch):
    app, ws = harness
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Workspace, "project_root", property(lambda ws: project))
    with FileLock(str(project / "turn.lock")), app.queue.lock:
        app.queue.enqueue(ws, message="instruction", mode="now")
        app.queue._tick()
        state = app.queue.snapshot(ws)
        assert state["active"] is None and len(state["queue"]) == 1
        assert not app.queue.processes
        app.queue.stop(ws)


def test_rewind_queue_reset_clears_pending_jobs_and_resume_links_durably(harness):
    app, ws = harness
    queue = app.queue
    queue.enqueue(ws, message="hold")
    until(lambda: read_jsonl(ws.root / "executions.jsonl"))
    queue.enqueue(ws, message="discard after rewind")
    with pytest.raises(QueueConflict, match="Stop the active turn"):
        queue.reset(ws)
    queue.stop(ws)
    until(lambda: queue.snapshot(ws)["active"] is None)
    assert queue.snapshot(ws)["resumable_job"]
    with FileLock(str(ws.root / "turn.lock")):
        with pytest.raises(QueueConflict, match="Stop the active turn"):
            queue.reset(ws)
        queue.reset(ws, turn_lock_held=True)
    state = queue.snapshot(ws)
    assert state == {"active": None, "queue": [], "paused": True, "last_job": None, "resumable_job": None}
    # Reload the persisted state without touching the worker receipts.
    with queue.lock:
        queue.states.pop(ws.id)
        assert queue.snapshot(ws) == state
    queue.resume(ws)
    queue._tick()
    assert not queue.processes
