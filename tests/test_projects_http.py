"""Project creation, membership and shared previews through the public API."""
import json
import threading
from http.client import HTTPConnection

import pytest

from vwb.web.server import Workbench, WorkbenchServer


@pytest.fixture
def http(tmp_path):
    app = Workbench(tmp_path)
    server = WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, *, auth=True):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"X-CSRF-Token": app.csrf_token} if auth else {})
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, json.loads(payload)
    try:
        yield app, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        app.queue.close()


def test_create_and_join_project_with_shared_workspace(http):
    app, request = http
    assert request("POST", "/api/projects", {"name": "Campaign"}, auth=False)[0] == 403
    status, created = request("POST", "/api/projects", {"name": "Campaign", "operation_id": "retry"})
    assert status == 201
    pid = created["project"]["id"]
    assert request("POST", "/api/projects", {"name": "Campaign", "operation_id": "retry"})[1] == created
    a = request("POST", "/api/workspaces", {})[1]["request_id"]
    aw = app.workspace(a)
    (aw.work / "brief.txt").write_text("shared")
    assert request("POST", f"/api/requests/{a}/project", {"project_id": pid})[0] == 200
    status, created_b = request("POST", "/api/workspaces", {"project_id": pid})
    assert status == 201
    b = created_b["request_id"]
    assert (app.workspace(b).work / "brief.txt").read_text() == "shared"
    listing = request("GET", "/api/requests")[1]
    assert listing["projects"] == [created["project"]]
    assert {row["project_id"] for row in listing["requests"]} == {pid}
    assert request("GET", f"/api/requests/{b}/features?q=brief")[1]["files"][0]["id"] == "brief.txt"
    assert request("GET", f"/api/requests/{b}")[1]["request"]["chat"] == []


def test_project_move_refuses_pending_turn_without_losing_files(http):
    app, request = http
    rid = request("POST", "/api/workspaces", {})[1]["request_id"]
    ws = app.workspace(rid)
    app.queue.stop(ws)
    app.queue.enqueue(ws, message="later")
    pid = request("POST", "/api/projects", {"name": "Campaign"})[1]["project"]["id"]
    status, result = request("POST", f"/api/requests/{rid}/project", {"project_id": pid})
    assert status == 409 and "queued" in result["error"]
    assert ws.project_root is None


def test_project_conflicts_and_unknown_destinations_are_actionable(http):
    app, request = http
    pid = request("POST", "/api/projects", {"name": "Campaign"})[1]["project"]["id"]
    a = request("POST", "/api/workspaces", {"project_id": pid})[1]["request_id"]
    b = request("POST", "/api/workspaces", {})[1]["request_id"]
    (app.workspace(a).work / "brief.txt").write_text("first")
    (app.workspace(b).work / "brief.txt").write_text("second")
    status, result = request("POST", f"/api/requests/{b}/project", {"project_id": pid})
    assert status == 409 and "conflict" in result["error"]
    assert app.workspace(b).load().project_id is None
    assert request("POST", "/api/workspaces", {"project_id": "../../private"})[0] == 400


@pytest.mark.parametrize("route,status", [("imports", 202), ("studio/uploads", 201)])
def test_file_submission_observes_completed_project_move(http, monkeypatch, route, status):
    app, request = http
    rid = request("POST", "/api/workspaces", {})[1]["request_id"]
    ws = app.workspace(rid)
    pid = app.projects.create("Campaign")["id"]
    called, submitted = threading.Event(), threading.Event()
    seen, responses = [], []
    def start(workspace, *args, **kwargs):
        seen.append(workspace.work)
        (workspace.work / "imported.txt").write_text("Imported after joining")
        called.set()
        return {"id": "fixture", "status": "ready"}
    if route == "imports":
        monkeypatch.setattr(app.local_imports, "start", start)
    else:
        monkeypatch.setattr("vwb.web.server.studio_assets.upload", start)
    def submit():
        submitted.set()
        responses.append(request("POST", f"/api/requests/{rid}/{route}", {"path": "/fixture"}))
    with app.mutation_lock:
        worker = threading.Thread(target=submit)
        worker.start()
        assert submitted.wait(1)
        assert not called.wait(.1)
        app.move_project(ws, {"project_id": pid})
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert responses[0][0] == status
    assert seen == [ws.work]
    assert (ws.work / "imported.txt").is_file()
    assert not (ws.root / "work/imported.txt").exists()
