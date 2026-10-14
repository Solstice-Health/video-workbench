"""Real loopback routes: preview isolation, byte ranges and OAuth boundaries."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from urllib.parse import quote

import pytest

from vwb.integrations import IntegrationError
from vwb.web import server as web
from vwb.web.queue import TurnQueue
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def api(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "r-canvas")
    for directory in SUBDIRS:
        (ws.root / directory).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="A canvas HTTP fixture"))
    (ws.work / "design").mkdir()
    (ws.work / "design" / "page.html").write_text('<img src="art.svg"><script>document.title="Preview"</script>')
    (ws.work / "design" / "art.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30"/>')
    (ws.work / "notes.txt").write_text("Readable notes")
    (ws.work / "demo.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"0" * 80)
    (ws.memory / "private.txt").write_text("Must stay private")
    calls = []

    class Service:
        def __init__(self, *, runs_dir):
            assert runs_dir == tmp_path

        def status(self):
            return {"items": [{"id": "solstice", "state": "unconfigured", "connected": False}]}

        def configure(self, body):
            calls.append(("configure", body))
            return self.status()

        def begin_auth(self, redirect_uri):
            calls.append(("connect", redirect_uri))
            return {"authorization_url": "https://login-solstice.us.auth0.com/authorize?state=fixture", "expires_at": 100}

        def begin_figma_auth(self):
            calls.append(("figma/connect",))
            return {"authorization_url": "https://www.figma.com/oauth?state=fixture"}

        def complete_auth(self, code, state, redirect_uri):
            calls.append(("callback", code, state, redirect_uri))
            if state != "one-use-state" or code != "good":
                raise IntegrationError("Never render provider detail or SECRET-CODE")
            return self.status()

        def refresh_catalog(self):
            calls.append(("refresh",))
            return self.status()

        def disconnect(self):
            calls.append(("disconnect",))
            return self.status()

    monkeypatch.setattr(web, "IntegrationService", Service)
    queue = TurnQueue(tmp_path)
    app = web.Workbench(tmp_path, queue=queue)
    server = web.WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def request(path, method="GET", body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    try:
        yield {"request": request, "ws": ws, "app": app, "port": port, "calls": calls,
               "headers": {"X-CSRF-Token": app.csrf_token, "Content-Type": "application/json"}}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        queue.close()


def metadata(api, path):
    status, _, raw = api["request"](f"/api/requests/r-canvas/workspace/file?path={quote(path)}")
    assert status == 200, raw
    return json.loads(raw)


def test_workspace_discovery_and_preview_metadata_use_safe_local_urls(api):
    status, _, raw = api["request"]("/api/requests/r-canvas/workspace?path=work")
    assert status == 200
    listing = json.loads(raw)
    assert {row["name"] for row in listing["entries"]} == {"design", "notes.txt", "demo.mp4"}
    entry = metadata(api, "work/notes.txt")
    assert entry["text"] == "Readable notes"
    assert entry["url"].startswith("/preview/r-canvas/")
    assert str(api["ws"].root) not in raw.decode()


def test_opaque_html_can_load_siblings_only_with_a_current_preview_capability(api):
    entry = metadata(api, "work/design/page.html")
    sibling = entry["url"].rsplit("/", 1)[0] + "/art.svg"
    status, headers, content = api["request"](sibling, headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
    assert status == 200 and content.startswith(b"<svg")
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "sandbox allow-scripts" in headers["Content-Security-Policy"]
    assert "allow-same-origin" not in headers["Content-Security-Policy"]
    assert "connect-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"
    assert api["request"](sibling, headers={"Sec-Fetch-Site": "cross-site"})[0] == 200
    invalid = sibling.replace(api["app"].preview_key("r-canvas"), "guessed")
    assert api["request"](invalid, headers={"Origin": "null"})[0] == 403
    assert api["request"](sibling, headers={"Origin": "https://attacker.invalid"})[0] == 403


@pytest.mark.parametrize("path", ["memory/private.txt", "work/../memory/private.txt", "work/.env", "work/link.txt"])
def test_preview_capability_never_expands_the_public_file_boundary(api, path):
    (api["ws"].work / "link.txt").symlink_to(api["ws"].memory / "private.txt")
    key = api["app"].preview_key("r-canvas")
    status, _, raw = api["request"](f"/preview/r-canvas/{key}/{quote(path, safe='/')}", headers={"Origin": "null"})
    assert status in {400, 403, 404}
    assert b"Must stay private" not in raw


def test_preview_capability_is_request_scoped_and_renewed_with_server(api):
    url = metadata(api, "work/notes.txt")["url"]
    changed = url.replace("/r-canvas/", "/r-other/")
    assert api["request"](changed)[0] == 403
    api["app"].csrf_token = "replacement-server-secret"
    assert api["request"](url)[0] == 403


def test_video_ranges_and_head_work_in_canvas(api):
    entry = metadata(api, "work/demo.mp4")
    status, headers, content = api["request"](entry["url"], headers={"Range": "bytes=4-11"})
    assert status == 206 and content == b"ftypisom"
    assert headers["Content-Range"].startswith("bytes 4-11/")
    assert headers["Content-Type"] == "video/mp4"
    assert api["request"](entry["url"], method="HEAD")[2] == b""


def test_pdf_preview_uses_native_reader_without_html_sandbox(api):
    (api["ws"].work / "document.pdf").write_bytes(b"%PDF-1.7\n")
    entry = metadata(api, "work/document.pdf")
    status, headers, content = api["request"](entry["url"])
    assert status == 200 and content.startswith(b"%PDF")
    assert headers["Content-Type"] == "application/pdf"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'self'"


def test_paged_pdf_route_preserves_workspace_and_origin_boundaries(api):
    import pypdfium2 as pdfium

    with pdfium.PdfDocument.new() as document:
        document.new_page(300, 400).close()
        document.save(api["ws"].work / "readable.pdf")
    route = "/api/requests/r-canvas/workspace/pdf?path=work/readable.pdf"
    status, _, raw = api["request"](route)
    value = json.loads(raw)
    assert status == 200 and value["pages"] == 1 and value["image"].startswith("data:image/png;base64,")
    assert api["request"](route + "&page=2")[0] == 400
    assert api["request"](route, headers={"Origin": "null"})[0] == 403
    assert api["request"](route.replace("work/readable.pdf", "memory/private.txt"))[0] == 403


@pytest.mark.parametrize("path", ["/api/bootstrap", "/api/requests/r-canvas/workspace", "/api/integrations"])
def test_opaque_preview_scripts_cannot_read_host_apis(api, path):
    assert api["request"](path, headers={"Origin": "null"})[0] == 403


@pytest.mark.parametrize("action", ["configure", "connect", "refresh", "disconnect", "figma/connect"])
def test_integration_mutations_require_csrf_and_use_host_owned_service(api, action):
    path = f"/api/integrations/{action}"
    assert api["request"](path, "POST", "{}")[0] == 403
    assert not api["calls"]
    assert api["request"](path, "POST", "{}", api["headers"])[0] == 200
    assert api["calls"][-1][0] == action
    if action == "connect":
        assert api["calls"][-1][1] == f"http://127.0.0.1:{api['port']}/api/integrations/solstice/callback"


def test_oauth_callback_allows_redirect_but_still_checks_host_and_one_use_state(api):
    path = "/api/integrations/solstice/callback?code=good&state=one-use-state"
    status, headers, html = api["request"](path, headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 200 and b'"ok": true' in html
    assert b"one-use-state" not in html and b"code=good" not in html
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    assert "nonce-" in headers["Content-Security-Policy"]
    status, _, html = api["request"](path.replace("one-use-state", "wrong"), headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 400 and b'"ok": false' in html and b"SECRET-CODE" not in html
    count = len(api["calls"])
    assert api["request"](path, headers={"Host": "attacker.invalid"})[0] == 403
    assert len(api["calls"]) == count
