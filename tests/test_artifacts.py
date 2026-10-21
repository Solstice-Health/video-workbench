"""General artifacts: immutable markup, bounded selections, pinned assets and confirmed feedback."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from vwb.artifacts import service
from vwb.web.server import Workbench, WorkbenchServer
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "r-artifacts")
    for directory in SUBDIRS:
        (workspace.root / directory).mkdir(parents=True, exist_ok=True)
    workspace.save(Request(id=workspace.id, message="Artifact test"))
    (workspace.work / "grid.html").write_text('<h1>Review</h1><img data-vwb-item="shot-1" data-vwb-asset="work/frame.svg">')
    (workspace.work / "frame.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    return workspace


def publish(ws, **kwargs):
    return service.publish(ws, html="grid.html", title="Review grid", actor="job_lead",
                           items=[{"id": "shot-1", "label": "Opening shot", "asset_path": "work/frame.svg"}], **kwargs)


def test_revision_preserves_html_and_asset_selection_contract(ws):
    first = publish(ws)
    (ws.work / "grid.html").write_text("<p>Updated</p>")
    second = publish(ws, artifact_id=first["id"])
    assert second["revision"] == 2
    assert "Review" in service.html_path(ws, first["id"], 1).read_text()
    assert service.get(ws, first["id"])["revision"] == 2
    assert len(service.listing(ws)) == 1
    rendered = service.render_html(ws, first, {"work/frame.svg": "/scoped/asset/0"}).decode()
    assert "parent.postMessage" in rendered and '"work/frame.svg": "/scoped/asset/0"' in rendered
    assert "csrf" not in rendered and "fetch(" not in rendered
    (ws.work / "frame.svg").write_text("changed")
    with service.open_file(ws, "work/frame.svg") as handle, pytest.raises(ValueError, match="changed"):
        service.check_asset(handle, first["assets"][0])


@pytest.mark.parametrize("path", ["../memory/private.html", "/tmp/x.html", "work/../memory/private.html"])
def test_publish_rejects_private_or_outside_html(ws, path):
    with pytest.raises((PermissionError, ValueError, FileNotFoundError)):
        service.publish(ws, html=path, title="Unsafe", actor="job_lead")


def test_publish_rejects_duplicate_items_and_secret_asset(ws):
    with pytest.raises(ValueError, match="unique"):
        service.publish(ws, html="grid.html", title="Review", actor="job_lead", items=[{"id":"a"},{"id":"a"}])
    with pytest.raises(PermissionError):
        service.publish(ws, html="grid.html", title="Review", actor="job_lead", assets=["memory/private.txt"])
    outside = ws.root.parent / "outside.svg"
    outside.write_text("secret")
    (ws.work / "symlink.svg").symlink_to(outside)
    with pytest.raises(PermissionError):
        service.publish(ws, html="grid.html", title="Review", actor="job_lead", assets=["work/symlink.svg"])


@pytest.mark.parametrize("payload", [
    {"item_id":"wrong"}, {"item_id":"shot-1","time_s":float("nan")},
    {"item_id":"shot-1","time_s":True}, {"item_id":None,"time_s":1},
    {"item_id":"shot-1","region":{"x":0.8,"y":0,"width":0.4,"height":1}},
    {"item_id":"shot-1","region":{"x":0,"y":0,"width":0,"height":1}},
    {"item_id":"shot-1","region":{"x":0,"y":0,"width":1,"height":float("inf")}},
])
def test_selection_rejects_unbound_or_invalid_metadata(ws, payload):
    with pytest.raises(ValueError):
        service.selection(publish(ws), payload)


def test_feedback_queues_exact_revision_and_is_idempotent(ws):
    row = publish(ws)
    calls = []
    def enqueue(message, mode):
        calls.append((message, mode))
        return {"id":"job-1"}
    body = {"revision":1,"item_id":"shot-1","text":"Crop this closer", "submission_id":"feedback-one",
            "region":{"x":0.1,"y":0.2,"width":0.3,"height":0.4},"time_s":2.5,"mode":"queue"}
    result = service.submit_feedback(ws, row["id"], body, enqueue)
    assert service.submit_feedback(ws, row["id"], body, enqueue) == result
    assert len(calls) == 1 and '"time_s": 2.5' in calls[0][0]
    assert "Opening shot" in calls[0][0] and "work/frame.svg" in calls[0][0]
    assert result["delivery"] == "queued"
    saved = service.feedback(ws, row["id"])
    assert saved["feedback"] == [result]
    assert service.feedback(ws, row["id"], after=saved["cursor"])["feedback"] == []
    assert not saved["has_more"]


def test_failed_enqueue_can_be_retried_without_false_sent_feedback(ws):
    row = publish(ws)
    body = {"revision":1,"text":"Keep going","submission_id":"feedback-retry"}
    def fail(*_):
        raise ValueError("Queue unavailable")
    with pytest.raises(ValueError, match="Queue unavailable"):
        service.submit_feedback(ws, row["id"], body, fail)
    assert service.feedback(ws, row["id"])["feedback"][0]["delivery"] == "pending"
    assert service.submit_feedback(ws, row["id"], body, lambda *_: {"id":"retry"})["job_id"] == "retry"


@pytest.fixture
def http_api(ws):
    class Queue:
        def __init__(self):
            self.calls = []
        def enqueue(self, workspace, **kwargs):
            self.calls.append((workspace.id, kwargs))
            return {"id":"queued-feedback"}
    app = Workbench(ws.root.parent, queue=Queue())
    server = WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, method="GET", body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()
    try:
        yield app, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        app.studio.close()


def test_http_preview_scope_ranges_csrf_and_feedback_delivery(ws, http_api):
    row = publish(ws)
    app, request = http_api
    api_root = f"/api/requests/{ws.id}/artifacts"
    status, _, raw = request(api_root)
    assert status == 200
    entry = json.loads(raw)["artifacts"][0]
    status, headers, raw = request(entry["url"], headers={"Origin":"null"})
    assert status == 200 and b"VWB" in raw
    csp = headers["Content-Security-Policy"]
    assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "form-action 'none'" in csp
    assert app.csrf_token.encode() not in raw
    asset_url = entry["url"].rsplit("/",1)[0] + "/asset/0"
    status, headers, raw = request(asset_url, headers={"Origin":"null","Range":"bytes=0-3"})
    assert status == 206 and raw == b"<svg"
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert request(entry["url"].replace(f"/{row['id']}/", "/another-artifact/"))[0] in {403,404}
    assert request(asset_url[:-1]+"1")[0] == 404
    assert request(entry["url"], headers={"Origin":"https://attacker.invalid"})[0] == 403
    assert request(api_root, headers={"Origin":"null"})[0] == 403
    endpoint = f"{api_root}/{row['id']}/feedback"
    payload = json.dumps({"revision":1,"item_id":"shot-1","text":"Make the crop tighter","submission_id":"http-feedback"})
    assert request(endpoint,"POST",payload)[0] == 403
    assert request(endpoint,"POST",payload,{"Origin":"null","X-CSRF-Token":app.csrf_token})[0] == 403
    status, _, raw = request(endpoint,"POST",payload,{"X-CSRF-Token":app.csrf_token})
    assert status == 202 and json.loads(raw)["feedback"]["job_id"] == "queued-feedback"
    assert len(app.queue.calls) == 1
    assert request(endpoint,"POST",payload,{"X-CSRF-Token":app.csrf_token})[0] == 202
    assert len(app.queue.calls) == 1
    assert "Make the crop tighter" in app.queue.calls[0][1]["message"]


def test_feedback_requires_revision_and_rejects_changed_reviewed_pixels(ws):
    row = publish(ws)
    with pytest.raises(ValueError, match="revision"):
        service.submit_feedback(ws, row["id"], {"text":"Closer","submission_id":"no-revision"}, lambda *_: {"id":"job"})
    (ws.work / "frame.svg").write_text("different pixels")
    with pytest.raises(ValueError, match="changed"):
        service.submit_feedback(ws, row["id"], {"revision":1,"item_id":"shot-1","text":"Closer","submission_id":"stale"}, lambda *_: {"id":"job"})
    assert not service.feedback(ws, row["id"])["feedback"]


def test_pending_feedback_retry_reconciles_an_uncertain_enqueue(ws):
    row = publish(ws)
    jobs = []
    body = {"revision":1,"item_id":"shot-1","text":"Keep this crop","submission_id":"uncertain"}
    def uncertain(*_):
        jobs.append({"id":"once"})
        raise RuntimeError("Process stopped after queue persistence")
    with pytest.raises(RuntimeError):
        service.submit_feedback(ws, row["id"], body, uncertain)
    assert service.feedback(ws, row["id"])["feedback"][0]["delivery"] == "pending"
    result = service.submit_feedback(ws, row["id"], body, lambda *_: jobs[0])
    assert result["job_id"] == "once" and result["delivery"] == "queued" and len(jobs) == 1
    with pytest.raises(ValueError, match="different"):
        service.submit_feedback(ws, row["id"], {**body,"text":"A changed message"}, lambda *_: jobs[0])
