from __future__ import annotations

import base64
import io
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from vwb.integrations import IntegrationError, IntegrationService
from vwb.integrations.artifacts import import_result
from vwb.integrations.service import CALLBACK_PATH, GATEWAYS, ISSUER
from vwb.integrations.transport import HTTP, MCP, Unauthorized
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace

CALLBACK = "http://127.0.0.1:8765" + CALLBACK_PATH
READ = {"name": "solstice_read", "description": "Read a file", "inputSchema": {
    "type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"], "additionalProperties": False},
    "annotations": {"readOnlyHint": True}}


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.response = {"access_token": "private-access", "refresh_token": "private-refresh", "expires_in": 3600,
                         "token_type": "Bearer"}
        self.status = 200

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.status, {}, self.response.copy()


class FakeMCP:
    def __init__(self):
        self.calls = []
        self.catalog = [READ.copy()]
        self.result = {"content": [{"type": "text", "text": "source evidence"}]}

    def execute(self, url, token, method, params=None):
        self.calls.append((url, token, method, params))
        return {"tools": self.catalog} if method == "tools/list" else self.result


@pytest.fixture
def service(tmp_path):
    value = IntegrationService(tmp_path / "host", http=FakeHTTP(), mcp=FakeMCP())
    value.configure({"client_id": "workbench-client", "redirect_uri": CALLBACK})
    return value


def login(service):
    auth = service.begin_auth(CALLBACK)
    query = parse_qs(urlsplit(auth["authorization_url"]).query)
    service.complete_auth("one-use-code", query["state"][0], CALLBACK)
    return query


def ws(tmp_path):
    workspace = Workspace(tmp_path / "request")
    workspace.work.mkdir(parents=True)
    workspace.ledger.mkdir()
    return workspace


def test_oauth_exact_audience_pkce_persistent_secret_free_status(service):
    query = login(service)
    assert query["audience"] == [GATEWAYS[0]] and query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [GATEWAYS[0]]
    assert set(query["scope"][0].split()) == {"mcp:connect", "openid", "email", "offline_access"}
    assert service.http.calls[0][1] == ISSUER + "oauth/token"
    assert service.http.calls[0][2]["form"]["code_verifier"]
    assert service.http.calls[0][2]["form"]["resource"] == GATEWAYS[0]
    assert "client_secret" not in service.http.calls[0][2]["form"]
    status = service.status()
    assert status["items"][0]["connected"] and status["items"][0]["persistent"]
    assert not status["items"][1]["connected"]
    assert "private-access" not in json.dumps(status) and "private-refresh" not in json.dumps(status)
    assert stat.S_IMODE(service.store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(service.store.root.stat().st_mode) == 0o700
    reopened = IntegrationService(service.store.root.parent.parent, http=service.http, mcp=service.mcp)
    assert reopened.status()["items"][0]["connected"]


def test_callback_state_wrong_expired_and_replay(service):
    auth = service.begin_auth(CALLBACK)
    state = parse_qs(urlsplit(auth["authorization_url"]).query)["state"][0]
    with pytest.raises(IntegrationError, match="state"):
        service.complete_auth("code", "wrong", CALLBACK)
    assert not service.http.calls
    service.complete_auth("code", state, CALLBACK)
    with pytest.raises(IntegrationError, match="state"):
        service.complete_auth("code", state, CALLBACK)
    assert len(service.http.calls) == 1
    service.begin_auth(CALLBACK)
    saved = service.store.read()
    saved["pending"]["expires_at"] = 0
    service.store.write(saved)
    with pytest.raises(IntegrationError, match="state"):
        service.complete_auth("code", saved["pending"]["state"], CALLBACK)


@pytest.mark.parametrize("url", ["https://evil.example/callback", "http://127.0.0.1:8765/other", CALLBACK + "?secret=x",
                                 "http://localhost.evil:8765" + CALLBACK_PATH, "http://user@localhost" + CALLBACK_PATH])
def test_callback_rejects_unregistered_shapes(service, url):
    with pytest.raises(IntegrationError):
        service.configure({"redirect_uri": url})


def test_gateway_and_client_identity_changes_forget_tokens(service):
    login(service)
    with pytest.raises(IntegrationError, match="documented"):
        service.configure({"gateway_url": "https://evil.example/mcp"})
    with pytest.raises(IntegrationError):
        service.configure({"client_secret": "no"})
    service.configure({"gateway_url": GATEWAYS[2]})
    assert "credentials" not in service.store.read()
    assert not service.status()["items"][0]["connected"]


def test_refresh_rotation_is_shared_locked_and_saved_before_mcp(service):
    login(service)
    saved = service.store.read()
    saved["credentials"]["expires_at"] = 0
    service.store.write(saved)
    service.http.calls.clear()
    service.http.response.update(access_token="rotated-access", refresh_token="rotated-refresh")
    original = service.mcp.execute
    mutex = threading.Lock()

    def assert_saved(url, token, method, params=None):
        assert service.store.read()["credentials"]["refresh_token"] == "rotated-refresh"
        with mutex:
            return original(url, token, method, params)

    service.mcp.execute = assert_saved
    other = IntegrationService(service.store.root.parent.parent, http=service.http, mcp=service.mcp)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda item: item.refresh_catalog(), [service, other]))
    assert len(service.http.calls) == 1
    assert service.http.calls[0][2]["form"]["refresh_token"] == "private-refresh"
    assert service.http.calls[0][2]["form"]["resource"] == GATEWAYS[0]
    assert service.store.read()["credentials"]["refresh_token"] == "rotated-refresh"


def test_invalid_refresh_requires_reconnect_no_repeat(service):
    login(service)
    saved = service.store.read()
    saved["credentials"]["expires_at"] = 0
    service.store.write(saved)
    service.http.status = 400
    service.http.response = {"error": "invalid_grant", "error_description": "private provider detail"}
    with pytest.raises(Unauthorized, match="Reconnect"):
        service.refresh_catalog()
    assert "credentials" not in service.store.read()
    assert "private provider detail" not in json.dumps(service.status())
    before = len(service.http.calls)
    with pytest.raises(Unauthorized):
        service.refresh_catalog()
    assert len(service.http.calls) == before


def test_disconnect_forgets_pending_and_tokens(service):
    login(service)
    service.begin_auth(CALLBACK)
    service.disconnect()
    assert set(service.store.read()) == {"config"}
    assert not service.status()["items"][0]["connected"]


def test_figma_requires_confirmed_user_grant_and_connect_is_ui_only(service, tmp_path):
    status_tool = {**READ, "name": "solstice_figma_status", "inputSchema": {"type": "object"}}
    connect_tool = {**READ, "name": "solstice_figma_connect", "inputSchema": {"type": "object"},
                    "annotations": {"readOnlyHint": True}}
    service.mcp.catalog += [status_tool, connect_tool]
    service.mcp.result = {"structuredContent": {"configured": True, "connected": False}}
    login(service)
    figma = service.status()["items"][1]
    assert figma["available"] and not figma["connected"] and figma["state"] == "disconnected"
    assert not any(t["name"] == "solstice_figma_connect" for t in service.tools()["tools"])
    with pytest.raises(IntegrationError, match="approved read"):
        service.call(ws(tmp_path), "solstice_figma_connect", {})
    original = service.mcp.execute

    def execute(url, token, method, params=None):
        if method == "tools/call" and params["name"] == "solstice_figma_connect":
            return {"structuredContent": {"authorization_url": "https://api.solsticehealth.co/mcp/figma/connect?ticket=" + "t" * 43, "expires_in": 300}}
        return original(url, token, method, params)

    service.mcp.execute = execute
    assert service.begin_figma_auth()["authorization_url"].startswith("https://api.solsticehealth.co/mcp/figma/connect?ticket=")
    service.mcp.result = {"structuredContent": {"configured": True, "connected": True}}
    service.refresh_catalog()
    assert service.status()["items"][1]["connected"]


def test_tool_advertisement_alone_is_not_figma_connection(service):
    service.mcp.catalog.append({**READ, "name": "solstice_figma_get_file"})
    login(service)
    assert not service.status()["items"][1]["connected"]
    assert not service.status()["items"][1]["available"]


@pytest.mark.parametrize("authorization_url", [
    "https://www.figma.com/oauth?state=" + "t" * 43,
    "https://evil.example/mcp/figma/connect?ticket=" + "t" * 43,
    "https://api.solsticehealth.co/elsewhere?ticket=" + "t" * 43,
    "https://api.solsticehealth.co/mcp/figma/connect?ticket=" + "t" * 43 + "&redirect=https://evil.example",
    "https://api.solsticehealth.co/mcp/figma/connect?ticket=" + "t" * 43 + "&ticket=" + "u" * 43,
])
def test_figma_auth_starts_only_at_exact_gateway_identity_handoff(service, authorization_url):
    service.mcp.catalog += [{**READ, "name": "solstice_figma_status"}, {**READ, "name": "solstice_figma_connect"}]
    original = service.mcp.execute

    def execute(url, token, method, params=None):
        if method == "tools/call" and params["name"] == "solstice_figma_status":
            return {"structuredContent": {"configured": True, "connected": False}}
        if method == "tools/call" and params["name"] == "solstice_figma_connect":
            return {"structuredContent": {"authorization_url": authorization_url}}
        return original(url, token, method, params)

    service.mcp.execute = execute
    login(service)
    with pytest.raises(IntegrationError, match="valid Figma sign-in"):
        service.begin_figma_auth()


def test_read_policy_schema_and_live_catalog_change(service, tmp_path):
    service.mcp.catalog += [{**READ, "name": "write", "annotations": {"readOnlyHint": False}},
                            {**READ, "name": "unknown", "annotations": {}},
                            {**READ, "name": "destructive", "annotations": {"readOnlyHint": True, "destructiveHint": True}}]
    login(service)
    assert [t["name"] for t in service.tools()["tools"]] == ["solstice_read"]
    service.configure({"read_tools": ["unknown", "write", "destructive"]})
    assert {t["name"] for t in service.tools()["tools"]} == {"solstice_read", "unknown"}
    workspace = ws(tmp_path)
    with pytest.raises(IntegrationError, match="schema"):
        service.call(workspace, "solstice_read", {"wrong": "file"})
    service.mcp.catalog[0]["annotations"] = {"readOnlyHint": False}
    with pytest.raises(IntegrationError, match="approved read"):
        service.call(workspace, "solstice_read", {"file": "one"})
    assert not any(call[2] == "tools/call" for call in service.mcp.calls)


def test_schema_cannot_fetch_remote_refs(service):
    service.mcp.catalog = [{**READ, "inputSchema": {"$ref": "http://localhost:9000/private"}}]
    with pytest.raises(IntegrationError, match="local definitions"):
        login(service)


def test_schema_cannot_change_base_url_for_local_references(service):
    service.mcp.catalog = [{**READ, "inputSchema": {"$id": "http://localhost:9000/private", "$ref": "#node"}}]
    with pytest.raises(IntegrationError, match="base URL"):
        login(service)


def test_tool_via_registry_imports_evidence_and_enforces_roles(service, tmp_path, monkeypatch):
    login(service)
    workspace = ws(tmp_path)
    ctx = ToolContext(workspace, "job_lead", "job_lead", None, state={"integration_service": service})
    registry = build_registry()
    result = registry.call(ctx, "integration_call", {"tool": "solstice_read", "arguments": {"file": "one"}})
    assert not result.is_error and result.artifacts
    artifact = result.data["artifacts"][0]
    assert (workspace.work / artifact["path"]).is_file()
    assert "integration.imported" in (workspace.ledger / "ledger.jsonl").read_text()
    assert "integration_call" in (workspace.ledger / "tools.jsonl").read_text()
    ctx.role = "unknown_role"
    assert registry.call(ctx, "integration_call", {"tool": "solstice_read", "arguments": {"file": "one"}}).is_error
    ctx.role = "job_lead"
    monkeypatch.setattr(registry, "mode", lambda capability: "block")
    assert registry.call(ctx, "integration_status", {}).is_error


def test_large_remote_tree_is_preserved_but_agent_text_and_data_are_bounded(service, tmp_path):
    login(service)
    tree = {"nodes": [{"name": "Design component", "text": "界" * 8000}] * 20}
    service.mcp.result = {"content": [{"type": "text", "text": json.dumps(tree, ensure_ascii=False)}],
                          "structuredContent": tree}
    workspace = ws(tmp_path)
    ctx = ToolContext(workspace, "job_lead", "job_lead", None, state={"integration_service": service})
    result = build_registry().call(ctx, "integration_call", {"tool": "solstice_read", "arguments": {"file": "one"}})
    assert not result.is_error and result.data["content_truncated"]
    assert len(result.text.encode()) < 64 * 1024
    assert len(json.dumps(result.data, ensure_ascii=False).encode()) < 64 * 1024
    saved = json.loads((workspace.work / result.data["full_response_path"]).read_text())
    assert saved["structuredContent"] == tree
    assert json.loads(saved["content"][0]["text"]) == tree
    assert result.artifacts == [item["sha256"] for item in result.data["artifacts"]]


def test_import_images_text_provenance_and_no_url_fetch(tmp_path):
    data = io.BytesIO()
    Image.new("RGB", (30, 20), "red").save(data, format="PNG")
    workspace = ws(tmp_path)
    result = import_result(workspace, "solstice_figma_get_images", {"file_key": "file"}, {"content": [
        {"type": "image", "data": base64.b64encode(data.getvalue()).decode(), "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "figma://file/node", "mimeType": "image/svg+xml",
                                           "blob": base64.b64encode(b'<svg onload="evil()"/>').decode()}},
        {"type": "resource_link", "uri": "http://127.0.0.1/secret", "name": "do not fetch"}]}, GATEWAYS[0])
    assert result["images"] == [data.getvalue()]
    assert len(result["artifacts"]) == 3
    assert result["artifacts"][1]["path"].endswith(".svg.txt")
    assert result["content"][2]["uri"] == "http://127.0.0.1/secret"
    assert result["gateway"] == GATEWAYS[0]


def test_import_rejects_symlink_boundary_and_invalid_image(tmp_path):
    workspace = ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.work / "integrations").symlink_to(outside, target_is_directory=True)
    with pytest.raises(IntegrationError, match="safely"):
        import_result(workspace, "read", {}, {"content": []}, GATEWAYS[0])
    assert not list(outside.iterdir())
    with pytest.raises(IntegrationError, match="preview image"):
        import_result(workspace, "read", {}, {"content": [{"type": "image", "data": "ZmFrZQ==", "mimeType": "image/png"}]}, GATEWAYS[0])


def test_transport_json_session_catalog_pagination_and_cleanup():
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif body.get("params", {}).get("cursor"):
            result = {"tools": [{**READ, "name": "second"}]}
        else:
            result = {"tools": [READ], "nextCursor": "next-page"}
        return httpx.Response(200, headers={"Mcp-Session-Id": "own-session"},
                              json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = MCP(HTTP(client)).execute(GATEWAYS[0], "secret", "tools/list")
    assert len(result["tools"]) == 2 and len(calls) == 5
    assert calls[1].headers["mcp-session-id"] == "own-session"
    assert calls[-1].method == "DELETE"
    assert all(str(call.url) == GATEWAYS[0] for call in calls)


def test_transport_sse_no_redirect_or_error_secret():
    def respond(request):
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = {"protocolVersion": "2025-06-18"} if body["method"] == "initialize" else {"content": []}
        event = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"event: message\ndata: {event}\n\n")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert MCP(HTTP(client)).execute(GATEWAYS[0], "secret", "tools/call", {}) == {"content": []}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="private-secret"))) as client:
        with pytest.raises(Unauthorized) as exc:
            MCP(HTTP(client)).execute(GATEWAYS[0], "secret", "tools/list")
        assert "private-secret" not in str(exc.value)
    seen = []

    def redirect(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/mcp"})

    with httpx.Client(transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(IntegrationError, match="302"):
            MCP(HTTP(client)).execute(GATEWAYS[0], "secret", "tools/list")
    assert len(seen) == 1


def test_transport_failure_does_not_replay_tool():
    count = []

    def respond(request):
        body = json.loads(request.content)
        count.append(body["method"])
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/call":
            raise httpx.ReadTimeout("private request token", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(IntegrationError, match="not replayed") as exc:
            MCP(HTTP(client)).execute(GATEWAYS[0], "secret", "tools/call", {})
    assert count.count("tools/call") == 1 and "private request token" not in str(exc.value)


def test_storage_symlink_rejected(tmp_path):
    (tmp_path / ".integrations").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(IntegrationError, match="symlink"):
        IntegrationService(tmp_path)
