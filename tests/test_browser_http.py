"""Browser HTTP boundaries, using a fake service and no Chrome/model processes."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from vwb.browser.service import BrowserConflict
from vwb.web import server as web_server
from vwb.web.queue import TurnQueue
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def browser_http(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "r-browser-http")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.root.name, message="Inspect the browser"))
    calls = []
    failure = {"error": None}

    class FakeBrowserSession:
        def __init__(self, workspace):
            self.ws = workspace
            calls.append(("construct", workspace.id))

        def status(self):
            calls.append(("status", self.ws.id))
            return {"request_id": self.ws.id, "running": False, "owner": "agent", "tabs": []}

        def ui_action(self, body):
            calls.append(("action", self.ws.id, body))
            if failure["error"] is not None:
                raise failure["error"]
            return {"request_id": self.ws.id, "accepted": body["action"]}

        def stream(self):
            calls.append(("stream", self.ws.id))
            yield "state", {"request_id": self.ws.id, "running": False}
            yield "frame", {"data": "small-fixture", "seq": 1, "metadata": {"deviceWidth": 1280}}

    monkeypatch.setattr(web_server, "BrowserSession", FakeBrowserSession)
    queue = TurnQueue(tmp_path)
    app = web_server.Workbench(tmp_path, queue=queue)
    server = web_server.WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def request(method, path, *, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    try:
        yield {
            "request": request, "ws": ws, "calls": calls, "failure": failure,
            "path": f"/api/requests/{ws.id}/browser", "port": port,
            "headers": {"X-CSRF-Token": app.csrf_token, "Content-Type": "application/json",
                        "Origin": f"http://127.0.0.1:{port}"},
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        queue.close()


def test_browser_status_get_does_not_start_a_browser(browser_http):
    api = browser_http
    status, headers, raw = api["request"]("GET", api["path"])
    assert status == 200
    assert json.loads(raw) == {"request_id": api["ws"].id, "running": False, "owner": "agent", "tabs": []}
    assert headers["Cache-Control"] == "no-store"
    assert api["calls"] == [("construct", api["ws"].id), ("status", api["ws"].id)]


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("headers", [
    {"Origin": "https://unrelated.example"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Host": "unrelated.example"},
])
def test_browser_reads_reject_foreign_requests_before_service_construction(browser_http, suffix, headers):
    api = browser_http
    status, _, raw = api["request"]("GET", api["path"] + suffix, headers=headers)
    assert status == 403 and json.loads(raw)["error"]
    assert api["calls"] == []


@pytest.mark.parametrize("change", ["missing-token", "wrong-token", "foreign-origin", "cross-site"])
def test_browser_mutations_require_csrf_and_origin_before_service_construction(browser_http, change):
    api = browser_http
    headers = dict(api["headers"])
    if change == "missing-token":
        headers.pop("X-CSRF-Token")
    elif change == "wrong-token":
        headers["X-CSRF-Token"] = "wrong"
    elif change == "foreign-origin":
        headers["Origin"] = "https://unrelated.example"
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    status, _, _ = api["request"]("POST", api["path"], body='{"action":"start"}', headers=headers)
    assert status == 403
    assert api["calls"] == []


@pytest.mark.parametrize(("method", "suffix"), [("GET", ""), ("GET", "/stream"), ("POST", "")])
def test_browser_unknown_request_never_constructs_service(browser_http, method, suffix):
    api = browser_http
    status, _, raw = api["request"](
        method, "/api/requests/r-missing/browser" + suffix,
        body='{"action":"start"}' if method == "POST" else None, headers=api["headers"],
    )
    assert status == 404 and json.loads(raw)["error"]
    assert api["calls"] == []


@pytest.mark.parametrize("body", [
    {"action": "start", "mode": "headed"},
    {"action": "control", "owner": "human"},
    {"action": "tab", "operation": "select", "tab_id": "t3"},
    {"action": "input", "events": [{"type": "input_mouse", "eventType": "mouseMoved", "x": 14, "y": 20}]},
    {"action": "close"},
])
def test_browser_mutations_use_the_resolved_request_service(browser_http, body):
    api = browser_http
    status, _, raw = api["request"]("POST", api["path"], body=json.dumps(body), headers=api["headers"])
    assert status == 200
    assert json.loads(raw) == {"request_id": api["ws"].id, "accepted": body["action"]}
    assert api["calls"] == [("construct", api["ws"].id), ("action", api["ws"].id, body)]


@pytest.mark.parametrize(("error", "expected"), [
    (BrowserConflict("Human control is required."), 409),
    (ValueError("Invalid browser action."), 400),
])
def test_browser_service_rejections_have_structured_http_responses(browser_http, error, expected):
    api = browser_http
    api["failure"]["error"] = error
    status, _, raw = api["request"](
        "POST", api["path"], body='{"action":"reload"}', headers=api["headers"],
    )
    assert status == expected
    assert json.loads(raw) == {"error": str(error)}


def test_browser_unexpected_service_failure_does_not_leak_private_endpoint(browser_http):
    api = browser_http
    api["failure"]["error"] = RuntimeError("wss://private.invalid/browser?token=private-value")
    status, _, raw = api["request"](
        "POST", api["path"], body='{"action":"start"}', headers=api["headers"],
    )
    assert status == 500 and json.loads(raw)["error"]
    assert b"private.invalid" not in raw and b"private-value" not in raw


@pytest.mark.parametrize("body", ["[]", "null", "not json"])
def test_browser_invalid_json_is_rejected_before_service_construction(browser_http, body):
    api = browser_http
    status, _, _ = api["request"]("POST", api["path"], body=body, headers=api["headers"])
    assert status == 400
    assert api["calls"] == []


def test_browser_stream_has_named_events_and_no_cache(browser_http):
    api = browser_http
    status, headers, raw = api["request"]("GET", api["path"] + "/stream")
    assert status == 200
    assert headers["Content-Type"].split(";")[0] == "text/event-stream"
    assert "no-store" in headers.get("Cache-Control", "") or "no-cache" in headers.get("Cache-Control", "")
    assert headers.get("X-Accel-Buffering") == "no"
    events = []
    for block in raw.decode().replace("\r\n", "\n").split("\n\n"):
        lines = block.splitlines()
        event = next((line.removeprefix("event:").strip() for line in lines if line.startswith("event:")), None)
        data = "\n".join(line.removeprefix("data:").strip() for line in lines if line.startswith("data:"))
        if event:
            events.append((event, json.loads(data)))
    assert events == [
        ("state", {"request_id": api["ws"].id, "running": False}),
        ("frame", {"data": "small-fixture", "seq": 1, "metadata": {"deviceWidth": 1280}}),
    ]
    assert api["calls"] == [("construct", api["ws"].id), ("stream", api["ws"].id)]


def test_graceful_shutdown_closes_only_managed_browsers_and_continues_after_failure(tmp_path, monkeypatch):
    closed = []
    for rid, managed in (("r-owned-one", True), ("r-owned-two", True), ("r-independent-cli", False)):
        ws = Workspace(tmp_path / rid)
        ws.root.mkdir()
        ws.save(Request(id=rid, message="Shutdown fixture"))
        private = ws.root / ".browser"
        private.mkdir()
        (private / "state.json").write_text(json.dumps({"state": "running", "ui_managed": managed}))

    class Session:
        def __init__(self, ws):
            self.ws = ws

        def ui_action(self, body):
            assert body == {"action": "close"}
            closed.append(self.ws.id)
            if self.ws.id == "r-owned-one":
                raise RuntimeError("fixture unavailable daemon")

    monkeypatch.setattr(web_server, "BrowserSession", Session)
    web_server.Workbench(tmp_path, queue=object()).close_browsers()
    assert set(closed) == {"r-owned-one", "r-owned-two"}
