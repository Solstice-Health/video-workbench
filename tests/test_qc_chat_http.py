"""QC routes, sealed finding visibility and durable editor handoff over HTTP."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest
import test_qc_chat_runtime as fixtures

from vwb.gateway import Gateway
from vwb.qc.chat import QCWorkspace
from vwb.qc.chat_store import QCStore
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.web.server import Workbench, WorkbenchServer


@pytest.fixture
def parent(tmp_path):
    return fixtures.parent.__wrapped__(tmp_path)


@pytest.fixture
def http(parent):
    app = Workbench(parent.root.parent)
    app.queue.stop(parent)
    qws = QCWorkspace(parent)
    qws.initialize()
    app.queue.stop(qws)
    server = WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, *, auth=True):
        conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"X-CSRF-Token": app.csrf_token,
                              "Cookie": f"vwb_qc_{server.server_port}={app.qc_token}"} if auth else {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data)
    try:
        yield request, app, parent, qws
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        app.queue.close()


def finding(ws, lane="shared", **extra):
    target = QCStore(ws).targets()[0]
    return {"target": "output:short@v1", "f0": 2, "f1": 4, "region": [0.1, 0.1, 0.3, 0.3],
            "pin": {key: target[key] for key in ("sha256", "canvas_file_sha256")},
            "expected": "The footnote fits", "observed": "Clipped footnote", "lane": lane, **extra}


def test_qc_routes_keep_history_queue_and_attachments_separate(http):
    request, app, ws, qws = http
    base = f"/api/requests/{ws.id}"
    assert request("GET", base + "/qc", auth=False)[0] == 403
    assert app.qc_token not in json.dumps(request("GET", "/api/bootstrap", auth=False)[1])
    assert request("POST", base + "/qc/messages", {"message": "Private checklist"}, auth=False)[0] == 403
    status, response = request("POST", base + "/qc/messages", {"message": "Private checklist"})
    assert status == 200 and response["job"]["status"] == "queued"
    assert not app.queue.snapshot(ws)["queue"]
    assert app.queue.snapshot(qws)["queue"][0]["message"] == "Private checklist"
    qws.add_chat("human", "AUDIT_ONLY_DISCUSSION")
    status, qc = request("GET", base + "/qc")
    assert status == 200 and len(qc["targets"]) == 1
    assert qc["targets"][0]["url"].endswith("/short/v1/short_v1.mp4")
    assert qc["request"]["chat"][-1]["text"] == "AUDIT_ONLY_DISCUSSION"
    status, edit = request("GET", base)
    assert status == 200 and "AUDIT_ONLY_DISCUSSION" not in json.dumps(edit)
    assert "Private checklist" not in json.dumps(edit)
    for path in (".qc-chat/items.json", ".qc-chat/runtime/request.json"):
        assert request("GET", f"/files/{ws.id}/{path}")[0] in {400, 403, 404}
    assert request("DELETE", base + "/qc/queue/" + response["job"]["id"])[0] == 200
    assert not app.queue.snapshot(qws)["queue"]


def test_handoff_explicit_snapshot_and_idempotent_retry(http):
    request, app, ws, qws = http
    base = f"/api/requests/{ws.id}/qc"
    shared = request("POST", base + "/items", finding(ws))[1]["item"]
    audit = request("POST", base + "/items", finding(ws, "audit", observed="AUDIT_SECRET"))[1]["item"]
    registry = build_registry()
    editor = ToolContext(ws, "job_lead", "job_lead", Gateway(ws))
    assert registry.call(editor, "qc_items_read", {}).data == {"items": []}
    assert request("POST", base + "/handoff", {"item_ids": [audit["id"]], "idempotency_key": "audit"})[0] == 400
    body = {"item_ids": [shared["id"]], "idempotency_key": "batch-1"}
    status, result = request("POST", base + "/handoff", body)
    assert status == 200 and result["batch"]["status"] == "sent"
    assert request("POST", base + "/handoff", body)[1] == result
    assert len(app.queue.snapshot(ws)["queue"]) == 1
    message = app.queue.snapshot(ws)["queue"][0]["message"]
    assert shared["id"] in message and "AUDIT_SECRET" not in message
    received = registry.call(editor, "qc_items_read", {}).data["items"]
    assert len(received) == 1 and received[0]["id"] == shared["id"]
    assert registry.call(editor, "qc_items_read", {"id": audit["id"]}).is_error
    assert request("POST", base + f"/items/{shared['id']}/status", {
        "status": "resolved", "expected_revision": 1, "note": "Private later note"})[0] == 200
    assert "Private later note" not in json.dumps(registry.call(editor, "qc_items_read", {}).data)
    assert request("POST", base + f"/items/{shared['id']}/status", {
        "status": "open", "expected_revision": 1})[0] == 400


def test_finding_rejects_invalid_anchor_and_sneaked_authority(http):
    request, app, ws, qws = http
    path = f"/api/requests/{ws.id}/qc/items"
    for invalid in (finding(ws, target="output:../secret@v1"), finding(ws, f1=999),
                    finding(ws, region=[0.9, 0, 0.5, 1]), finding(ws, actor="human:admin"),
                    finding(ws, pin=None)):
        assert request("POST", path, invalid)[0] == 400


def test_attachment_submission_retry_reuses_original_refs(http, tmp_path, monkeypatch):
    request, app, ws, qws = http
    file = tmp_path / "checklist.txt"
    file.write_text("Check the footnote")
    monkeypatch.setattr(app, "attachment_paths", lambda ids: [file])
    path = f"/api/requests/{ws.id}/qc/messages"
    body = {"message": "QC brief", "attachments": ["uploaded-checklist"], "idempotency_key": "message-1"}
    status, first = request("POST", path, body)
    assert status == 200, first
    assert request("POST", path, body) == (200, first)
    assert len(app.queue.snapshot(qws)["queue"]) == 1
    assert len(list((qws.root / ".attachments").glob("*"))) == 1
    assert request("POST", path, {**body, "message": "Changed brief"})[0] == 400


def test_launch_cookie_is_out_of_band(http):
    request, app, ws, qws = http
    # A CSRF token from bootstrap alone never grants read access to private QC.
    server = WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", f"/?qc_access={app.qc_token}")
        response = conn.getresponse()
        assert response.status == 303
        cookie = response.getheader("Set-Cookie")
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert response.getheader("Location") == "/"
        response.read()
        conn.request("GET", f"/api/requests/{ws.id}/qc", headers={"Cookie": cookie.split(";", 1)[0]})
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
