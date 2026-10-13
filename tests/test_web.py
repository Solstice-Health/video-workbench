"""The browser control plane, exercised without a model call or Docker."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

from vwb.util.jsonio import read_json, write_json
from vwb.web.queue import QueueConflict, TurnQueue
from vwb.web.server import Workbench, WorkbenchServer, safe_child
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


def make_workspace(root: Path, rid: str = "r-test") -> Workspace:
    ws = Workspace(root / rid)
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=rid, message="Make a short video"))
    return ws


def until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.02)
    pytest.fail("Timed out waiting for queue state")


@pytest.fixture
def worker(tmp_path):
    script = tmp_path / "fake_worker.py"
    script.write_text('''import json, pathlib, sys, time
p = pathlib.Path(sys.argv[2]); job = json.loads(p.read_text())
log = pathlib.Path(job['request']) / 'execution.jsonl'
def event(action):
    with log.open('a') as fh:
        fh.write(json.dumps({'action': action, 'message': job['message']}) + '\\n')
try:
    event('start')
    time.sleep(30 if job['message'] == 'hold' else .1)
    event('finish')
except KeyboardInterrupt:
    time.sleep(.15)
    event('stopped')
    sys.exit(130)
''')
    return [sys.executable, str(script)]


def execution(ws):
    path = ws.root / "execution.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_serial_queue_and_remove(tmp_path, worker):
    ws = make_workspace(tmp_path)
    queue = TurnQueue(tmp_path, command=worker)
    try:
        queue.stop(ws)
        queue.enqueue(ws, message="one")
        removed = queue.enqueue(ws, message="remove me")
        queue.enqueue(ws, message="two")
        queue.cancel(ws, removed["id"])
        assert not execution(ws)
        queue.resume(ws)
        until(lambda: len(execution(ws)) == 4)
        assert execution(ws) == [{"action": a, "message": m} for m in ["one", "two"] for a in ["start", "finish"]]
    finally:
        queue.close()


def test_send_now_waits_for_interrupt_cleanup_then_resumes_same_request(tmp_path, worker):
    ws = make_workspace(tmp_path)
    queue = TurnQueue(tmp_path, command=worker)
    try:
        first = queue.enqueue(ws, message="hold")
        until(lambda: execution(ws))
        queue.enqueue(ws, message="later")
        queue.enqueue(ws, message="steer", mode="now")
        until(lambda: len(execution(ws)) == 6)
        assert execution(ws) == [
            {"action": "start", "message": "hold"}, {"action": "stopped", "message": "hold"},
            {"action": "start", "message": "steer"}, {"action": "finish", "message": "steer"},
            {"action": "start", "message": "later"}, {"action": "finish", "message": "later"},
        ]
        state = read_json(ws.memory / "web_queue.json")
        assert next(j for j in state["jobs"] if j["id"] == first["id"])["status"] == "stopped"
    finally:
        queue.close()


def test_stop_pauses_pending_jobs_and_restart_requires_resume(tmp_path, worker):
    ws = make_workspace(tmp_path)
    queue = TurnQueue(tmp_path, command=worker)
    queue.enqueue(ws, message="hold")
    until(lambda: execution(ws))
    queue.enqueue(ws, message="later")
    queue.stop(ws)
    until(lambda: queue.snapshot(ws)["active"] is None)
    queue.close()
    resumed = TurnQueue(tmp_path, command=worker)
    try:
        assert resumed.snapshot(ws)["paused"]
        time.sleep(.3)
        assert [e["message"] for e in execution(ws)] == ["hold", "hold"]
        resumed.resume(ws)
        until(lambda: len(execution(ws)) == 4)
        assert execution(ws)[-1] == {"action": "finish", "message": "later"}
    finally:
        resumed.close()


def test_only_one_server_owns_queue(tmp_path):
    first = TurnQueue(tmp_path)
    try:
        with pytest.raises(QueueConflict):
            TurnQueue(tmp_path)
    finally:
        first.close()


def test_paths_reject_traversal_and_symlink(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (allowed / "link.txt").symlink_to(outside)
    for value in ["../outside.txt", str(outside), "link.txt", "file\\..\\outside.txt", "x\x00"]:
        with pytest.raises(ValueError):
            safe_child(allowed, value)


@pytest.fixture
def app(tmp_path, worker):
    ws = make_workspace(tmp_path)
    queue = TurnQueue(tmp_path, command=worker)
    queue.stop(ws)
    app = Workbench(tmp_path, queue=queue)
    try:
        yield app, ws
    finally:
        queue.close()


def test_request_reads_do_not_default_old_questions(app):
    app, ws = app
    ticket = {"id": "ask-old", "status": "open", "question": "Keep the ribbon?", "created": "2020-01-01",
              "deadline": "2020-01-01T00:00:00+00:00", "on_timeout": "use_recommended"}
    path = ws.asks / "tickets" / "ask-old.json"
    write_json(path, ticket)
    before = path.read_bytes()
    assert app.snapshot(ws)["asks"][0]["status"] == "open"
    assert app.requests()[0]["open_questions"] == 1
    assert path.read_bytes() == before
    assert not (ws.ledger / "ledger.jsonl").exists()


def test_artifacts_cannot_expose_private_request_files(app):
    app, ws = app
    secret = ws.memory / "credentials.json"
    secret.write_text('{"secret":"no"}')
    (ws.media / "alias.json").symlink_to(secret)
    with pytest.raises(ValueError):
        app.file(ws.id, "memory/credentials.json")
    with pytest.raises(ValueError):
        app.file(ws.id, "media/alias.json")
    with pytest.raises(ValueError):
        app.workspace("../r-test")


def test_answer_is_signed_once_and_rejected_before_side_effects(app, monkeypatch):
    app, ws = app
    monkeypatch.setattr("vwb.workspace.ledger.control_key", lambda: b"test-key")
    ticket = {"id": "ask-one", "status": "open", "question": "Keep the ribbon?", "kind": "choose",
              "options": [{"id": "yes", "label": "Keep it"}, {"id": "no", "label": "Remove it"}],
              "lane": "editor", "key": "ribbon", "created": "2020-01-01"}
    path = ws.asks / "tickets" / "ask-one.json"
    write_json(path, ticket)
    with pytest.raises(ValueError):
        app.answer(ws, {"ticket": "ask-one", "option": "yes", "text": "x" * 100_000})
    assert read_json(path)["status"] == "open"
    assert not (ws.ledger / "ledger.jsonl").exists()
    result = app.answer(ws, {"ticket": "ask-one", "option": "yes", "text": "Only in the intro."})
    assert result["ticket"]["status"] == "answered"
    with pytest.raises(QueueConflict):
        app.answer(ws, {"ticket": "ask-one", "option": "yes"})
    assert len(app.queue.snapshot(ws)["queue"]) == 1
    ledger = [json.loads(line) for line in (ws.ledger / "ledger.jsonl").read_text().splitlines()]
    assert sum(e["kind"] == "human.answer" for e in ledger) == 1


def test_worker_interrupts_itself_if_ui_parent_exits(monkeypatch):
    from vwb.web.worker import _watch_parent
    calls = []
    monkeypatch.setattr("vwb.web.worker.os.getppid", lambda: 1)
    monkeypatch.setattr("vwb.web.worker.os.kill", lambda pid, sig: calls.append((pid, sig)))
    _watch_parent(123456, threading.Event())
    assert len(calls) == 1


def test_interrupt_reaps_process_group_exit_race(monkeypatch):
    import signal
    from unittest.mock import Mock
    proc = Mock(pid=12345)
    proc.poll.return_value = 0
    monkeypatch.setattr("vwb.web.queue.os.killpg", Mock(side_effect=PermissionError))
    TurnQueue._signal_group(proc, signal.SIGINT)
    proc.send_signal.assert_not_called()
    proc.poll.return_value = None
    TurnQueue._signal_group(proc, signal.SIGINT)
    proc.send_signal.assert_called_once_with(signal.SIGINT)


def test_question_timeout_cannot_overwrite_concurrent_answer(app, monkeypatch):
    from vwb.ask.broker import Broker
    app, ws = app
    monkeypatch.setattr("vwb.workspace.ledger.control_key", lambda: b"test-key")
    ticket = {"id": "ask-race", "status": "open", "question": "Keep it?", "kind": "choose",
              "options": [{"id": "yes"}, {"id": "no"}], "recommended": "yes", "on_timeout": "use_recommended",
              "deadline": "2020-01-01T00:00:00+00:00", "lane": "editor", "key": "race", "created": "2020-01-01"}
    path = ws.asks / "tickets" / "ask-race.json"
    write_json(path, ticket)
    poller, answerer = Broker(ws), Broker(ws)
    snapshot_read, let_timeout_finish = threading.Event(), threading.Event()
    real_timeouts = poller._apply_timeouts
    def delayed_timeouts(tickets):
        snapshot_read.set()
        assert let_timeout_finish.wait(3)
        real_timeouts(tickets)
    monkeypatch.setattr(poller, "_apply_timeouts", delayed_timeouts)
    polling = threading.Thread(target=poller.tickets)
    answering = threading.Thread(target=lambda: answerer.answer("ask-race", "no"))
    polling.start()
    assert snapshot_read.wait(3)
    answering.start()
    let_timeout_finish.set()
    polling.join(3)
    answering.join(3)
    assert not polling.is_alive() and not answering.is_alive()
    final = read_json(path)
    assert final["status"] == "answered" and final["answer"]["option"] == "no"


def test_explicit_compact_starts_paused_idle_queue_but_rejects_work(app, monkeypatch):
    from filelock import FileLock
    app, ws = app
    monkeypatch.setattr("vwb.web.server.context_usage", lambda ws: {"compact_available": True})
    with app.queue.lock:
        assert app.queue.snapshot(ws)["paused"]
        job = app.compact(ws)
        assert job["kind"] == "compact" and not app.queue.snapshot(ws)["paused"]
        with pytest.raises(QueueConflict, match="queued messages"):
            app.compact(ws)
        app.queue.cancel(ws, job["id"])
        app.queue.stop(ws)
        with FileLock(str(ws.root / "turn.lock")), pytest.raises(QueueConflict, match="current turn"):
            app.compact(ws)


def test_keepalive_disconnect_is_quiet_but_other_errors_propagate(monkeypatch):
    from http.server import BaseHTTPRequestHandler
    from unittest.mock import Mock

    from vwb.web.server import Handler
    handler = Handler.__new__(Handler)
    for error in [ConnectionResetError, BrokenPipeError]:
        monkeypatch.setattr(BaseHTTPRequestHandler, "handle", Mock(side_effect=error))
        handler.handle()
        assert handler.close_connection
    monkeypatch.setattr(BaseHTTPRequestHandler, "handle", Mock(side_effect=RuntimeError("real bug")))
    with pytest.raises(RuntimeError, match="real bug"):
        handler.handle()


@pytest.fixture
def http(app):
    app, ws = app
    server = WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result
    try:
        yield request, app, ws, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_csrf_origin_and_host_protection(http):
    request, app, ws, port = http
    status, _, body = request("GET", "/api/bootstrap")
    assert status == 200 and json.loads(body)["csrf_token"] == app.csrf_token
    mutation = f"/api/requests/{ws.id}/messages"
    assert request("POST", mutation, '{"message":"Hi"}')[0] == 403
    headers = {"X-CSRF-Token": app.csrf_token, "Origin": "https://evil.example"}
    assert request("POST", mutation, '{"message":"Hi"}', headers)[0] == 403
    assert request("GET", "/api/bootstrap", headers={"Host": f"evil.example:{port}"})[0] == 403
    headers["Origin"] = f"http://127.0.0.1:{port}"
    assert request("POST", mutation, '{"message":"Hi"}', headers)[0] == 202
    assert app.queue.snapshot(ws)["queue"][0]["message"] == "Hi"


def test_model_settings_routes_freeze_queue_and_never_return_keys(http, monkeypatch):
    from vwb import config
    request, app, ws, _ = http
    monkeypatch.setattr(config, "RUNS_DIR", app.runs_dir)
    env = app.runs_dir / "fixture.env"
    env.write_text("OPENAI_API_KEY=fixture-openai-key\nOPENROUTER_API_KEY=fixture-openrouter-key\n")
    monkeypatch.setenv("VWB_ENV_FILE", str(env))
    for key in config.PROVIDERS.values():
        monkeypatch.delenv(key, raising=False)
    config.secrets.cache_clear()
    headers = {"X-CSRF-Token": app.csrf_token, "Content-Type": "application/json"}
    def post(path, body):
        status, _, content = request("POST", path, json.dumps(body), headers)
        return status, json.loads(content)
    try:
        cat = json.loads(request("GET", "/api/models")[2])
        assert "fixture-openai-key" not in json.dumps(cat)
        assert "gpt-6-astra" in json.dumps(cat)
        route = f"/api/requests/{ws.id}/model"
        assert post(route, {"selection": {"model_id": "sol61", "context_tokens": 300000, "effort": "high"}})[0] == 200
        assert post(f"/api/requests/{ws.id}/messages", {"message": "Use this model"})[0] == 202
        assert post(route, {"selection": {"model_id": "astra"}})[0] == 200
        queued = app.queue.snapshot(ws)["queue"][0]
        assert queued["main_config"]["model"] == "gpt-6.1-sol"
        assert queued["main_config"]["effort"] == "high" and queued["main_config"]["context_window"] == 300000
        selection = json.loads(request("GET", route)[2])
        assert selection["selection"]["model_id"] == "astra" and selection["model"]["model"] == "gpt-6-astra"
        assert post("/api/models/preferences", {"enabled_model_ids": []})[0] == 400
        status, updated = post("/api/providers/google", {"action": "save", "api_key": "fixture-new-google-key"})
        assert status == 200 and "fixture-new-google-key" not in json.dumps(updated)
        assert any(p["configured"] for p in updated["providers"] if p["id"] == "google")
        assert request("GET", "/.keys/provider_keys.json")[0] == 404
        assert request("POST", "/api/providers/google", '{"action":"clear"}')[0] == 403
        assert post("/api/providers/google", {"action": "clear"})[0] == 200
    finally:
        config.secrets.cache_clear()


def test_http_range_and_no_private_files(http):
    request, _, ws, _ = http
    video = ws.media / "source.mp4"
    video.write_bytes(b"0123456789")
    url = f"/files/{ws.id}/media/source.mp4"
    status, headers, body = request("GET", url, headers={"Range": "bytes=2-5"})
    assert status == 206 and body == b"2345" and headers["Content-Range"] == "bytes 2-5/10"
    assert request("GET", url, headers={"Range": "bytes=-3"})[2] == b"789"
    assert request("GET", url, headers={"Range": "bytes=99-"})[0] == 416
    assert request("HEAD", url)[2] == b""
    assert request("GET", f"/files/{ws.id}/media/%2e%2e/memory/web_queue.json")[0] == 400


def test_upload_preserves_name_and_rejects_nonvideo(http):
    request, app, _, _ = http
    headers = {"X-CSRF-Token": app.csrf_token, "X-Filename": "My%20source.mp4"}
    status, _, body = request("POST", "/api/uploads", b"video-bytes", headers)
    upload = json.loads(body)["upload"]
    assert status == 201 and upload["name"] == "My source.mp4"
    assert app.attachment_paths([upload["id"]])[0].name == "My source.mp4"
    headers["X-Filename"] = "secret.py"
    assert request("POST", "/api/uploads", b"bad", headers)[0] == 400


def test_upload_and_create_do_not_attach_source_twice(http):
    request, app, _, _ = http
    headers = {"X-CSRF-Token": app.csrf_token, "X-Filename": "source.mp4"}
    _, _, data = request("POST", "/api/uploads", b"video-bytes", headers)
    upload = json.loads(data)["upload"]
    status, _, data = request("POST", "/api/requests", json.dumps({
        "message": "Make a short", "attachments": [upload["id"]]}), headers)
    result = json.loads(data)
    assert status == 201 and result["request"]["id"] == result["request_id"]
    ws = app.workspace(result["request_id"])
    assert len(ws.load().sources) == 1
    assert Path(ws.load().sources[0].path).read_bytes() == b"video-bytes"
    assert result["job"]["attachments"] == []  # already staged at intake, not reattached by worker
    assert not result["job"]["skip_index"]


def test_http_feature_catalog_and_context_submission(http):
    request, app, ws, port = http
    (ws.work / 'planning').mkdir()
    status, _, raw = request('GET', '/api/features')
    assert status == 200 and {s['id'] for s in json.loads(raw)['skills']} == {'clip', 'remake', 'shorts'}
    status, _, raw = request('GET', f'/api/requests/{ws.id}/features')
    assert status == 200 and 'planning' in {f['id'] for f in json.loads(raw)['folders']}
    csrf = json.loads(request('GET', '/api/bootstrap')[2])['csrf_token']
    headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}
    app.queue.stop(ws)
    context = {'skills': ['remake'], 'folders': ['planning']}
    status, _, raw = request('POST', f'/api/requests/{ws.id}/messages',
                            json.dumps({'message': 'Improve the intro', 'context': context}), headers)
    assert status == 202 and json.loads(raw)['job']['context'] == context
    status, _, raw = request('POST', f'/api/requests/{ws.id}/messages',
                            json.dumps({'message': 'Review', 'context': {'folders': ['../media']}}), headers)
    assert status == 400
    assert len(app.queue.snapshot(ws)['queue']) == 1
