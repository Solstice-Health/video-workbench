"""Message history operations and attachment-preserving edit dispatch over HTTP."""
import json
import sys
import threading
import time
from http.client import HTTPConnection

import pytest

from vwb.web.queue import TurnQueue
from vwb.web.server import Workbench, WorkbenchServer
from vwb.workspace import attachments


@pytest.fixture
def history_http(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text('''import json, sys
from pathlib import Path
from vwb.workspace import Workspace
job = json.loads(Path(sys.argv[2]).read_text())
ws = Workspace(Path(job['request']))
ws.add_chat('human', job['message'], context=job.get('context'), attachments=job.get('attachment_refs'))
ws.add_chat('job_lead', 'Fixture reply')
''')
    queue = TurnQueue(tmp_path, command=[sys.executable, str(script)])
    app = Workbench(tmp_path, queue=queue)
    rid = app.draft_workspace()["request_id"]
    ws = app.workspace(rid)
    files = []
    for i in range(2):
        path = tmp_path / f"checklist-{i}.txt"
        path.write_text(f"checklist {i}")
        files.append(path)
    refs = attachments.ingest(ws, files)
    ws.add_chat("human", "Original", attachments=[refs[0]])
    ws.add_chat("job_lead", "First reply")
    ws.add_chat("human", "Future", attachments=[refs[1]])
    ws.add_chat("job_lead", "Future reply")
    server = WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(body):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request("POST", f"/api/requests/{rid}/history", body=json.dumps(body), headers={"X-CSRF-Token": app.csrf_token})
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data
    try:
        yield app, ws, request, refs
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        queue.close()


def wait_idle(app, ws):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = app.queue.snapshot(ws)
        if not state["active"] and not state["queue"]:
            return
        time.sleep(.02)
    pytest.fail("Fixture turn did not finish")


def test_fork_through_message_preserves_selected_attachment_and_project(history_http):
    app, ws, request, refs = history_http
    pid = app.projects.create("Campaign")["id"]
    app.projects.move(ws, pid)
    before = ws.load()
    status, result = request({"action": "fork", "message_id": before.chat[1].id,
                              "expected_revision": before.history_revision, "operation_id": "fork"})
    assert status == 200, result
    fork = app.workspace(result["request_id"])
    assert fork.work == ws.work and fork.load().project_id == pid
    assert [turn.text for turn in fork.load().chat] == ["Original", "First reply"]
    assert attachments.original_path(fork, refs[0]).read_text() == "checklist 0"
    assert not (fork.root / ".attachments" / refs[1]["id"]).exists()
    assert ws.load() == before


@pytest.mark.parametrize("fork", [False, True])
def test_edit_sends_once_and_preserves_message_attachments(history_http, fork):
    app, ws, request, refs = history_http
    before = ws.load()
    body = {"action": "edit", "message_id": before.chat[0].id, "text": "Revised", "fork": fork,
            "expected_revision": before.history_revision, "operation_id": "edit"}
    status, result = request(body)
    assert status == 200, result
    target = app.workspace(result["request_id"])
    wait_idle(app, target)
    assert [t.text for t in target.load().chat] == ["Revised", "Fixture reply"]
    assert target.load().chat[0].attachments[0]["id"] == refs[0]["id"]
    assert request(body) == (200, result)
    assert len(target.load().chat) == 2
    assert ws.load() == before if fork else len(ws.load().chat) == 2


def test_rewind_is_revision_checked_and_clears_future_resumption(history_http):
    app, ws, request, _ = history_http
    before = ws.load()
    stale = {"action": "rewind", "message_id": before.chat[1].id,
             "expected_revision": before.history_revision - 1, "operation_id": "stale"}
    assert request(stale)[0] == 409
    assert ws.load() == before
    status, result = request({**stale, "operation_id": "rewind", "expected_revision": before.history_revision})
    assert status == 200, result
    assert len(ws.load().chat) == 2
    assert app.queue.snapshot(ws)["resumable_job"] is None
    assert result["history"]["filesystem"] == "current"
    assert (ws.root / ".conversation-history").is_dir()


def test_retry_recovers_enqueued_edit_after_http_receipt_write_failure(history_http, monkeypatch):
    from vwb.workspace import checkpoints
    app, ws, request, _ = history_http
    before = ws.load()
    body = {"action": "edit", "message_id": before.chat[0].id, "text": "Recovered edit",
            "expected_revision": before.history_revision, "operation_id": "recover"}
    original = checkpoints.durable_json
    def fail_response(path, value):
        if path.parent.name == "http" and value.get("response"):
            raise OSError("injected response write failure")
        return original(path, value)
    monkeypatch.setattr(app.queue, "_start", lambda *args: None)
    monkeypatch.setattr(checkpoints, "durable_json", fail_response)
    assert request(body)[0] == 500
    app.queue.stop(ws)  # equivalent to a recovered paused job after restart
    assert len(app.queue.snapshot(ws)["queue"]) == 1
    monkeypatch.setattr(checkpoints, "durable_json", original)
    status, result = request(body)
    assert status == 200, result
    assert result["job"]["message"] == "Recovered edit"
    assert len(app.queue.snapshot(ws)["queue"]) == 1
    assert len(ws.load().chat) == 0
