"""Exercise direct connections over real HTTP through the installed MCP SDK.

The fixture deliberately uses an authorization server under /auth and a token
endpoint under /auth/issue, so restart/refresh cannot accidentally rely on the
resource origin's conventional /token endpoint.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import stat
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from vwb.integrations import IntegrationError
from vwb.integrations.remote_mcp import RemoteMCP

CALLBACK = "http://127.0.0.1:8765/api/integrations/mcp/callback"
READ_TOOL = {
    "name": "read_note",
    "description": "Read the selected note.",
    "inputSchema": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}
WRITE_TOOL = {
    "name": "write_note",
    "description": "Write a note after the user enables this tool.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}


class WireMCP:
    """Small standards-shaped peer; no production transport or auth is mocked."""

    def __init__(self):
        self.requests = []
        self.counts = Counter()
        self.lock = threading.Lock()
        self.clients = {}
        self.codes = {}
        self.tokens = set()
        self.refresh_tokens = set()
        self.ttl = 3600
        self.allow_registration = True
        self.echo_authorization = False
        self.echo_secrets = False
        self.issued_secrets = []
        self.registration_secret = ""
        self.registration_error = None
        self.registration_error_status = 400
        self.registration_error_code = "invalid_client_metadata"
        self.catalog = [copy.deepcopy(READ_TOOL), copy.deepcopy(WRITE_TOOL)]
        self.violation = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def send(self, status, value=None, headers=None):
                body = b"" if value is None else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if value is not None:
                    self.send_header("Content-Type", "application/json")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def record(self, body=None):
                parsed = urlsplit(self.path)
                with owner.lock:
                    owner.requests.append({
                        "method": self.command, "path": parsed.path, "body": body,
                        "authorization": self.headers.get("Authorization"),
                        "query": parse_qs(parsed.query),
                    })
                    owner.counts[(self.command, parsed.path)] += 1
                return parsed

            def do_GET(self):
                parsed = self.record()
                path = parsed.path
                if path == "/.well-known/oauth-protected-resource/mcp":
                    return self.send(200, {
                        "resource": owner.protected_url,
                        "authorization_servers": [owner.issuer],
                        "scopes_supported": ["notes:read", "notes:write"],
                    })
                if path in {
                    "/.well-known/oauth-authorization-server/auth",
                    "/auth/.well-known/oauth-authorization-server",
                    "/auth/.well-known/openid-configuration",
                }:
                    metadata = {
                        "issuer": owner.issuer,
                        "authorization_endpoint": owner.base + "/auth/authorize",
                        "token_endpoint": owner.base + "/auth/issue",
                        "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_methods_supported": ["none"],
                        "code_challenge_methods_supported": ["S256"],
                        "authorization_response_iss_parameter_supported": True,
                        "scopes_supported": ["notes:read", "notes:write"],
                    }
                    if owner.allow_registration:
                        metadata["registration_endpoint"] = owner.base + "/auth/register"
                    return self.send(200, metadata)
                if path == "/auth/authorize":
                    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                    client = owner.clients.get(query.get("client_id"))
                    if (
                        not client or query.get("redirect_uri") not in client["redirect_uris"]
                        or query.get("code_challenge_method") != "S256"
                        or not query.get("state") or not query.get("code_challenge")
                    ):
                        owner.violation = "Authorization request omitted a valid client, redirect, state or PKCE."
                        return self.send(400, {"error": "invalid_request"})
                    code = f"one-use-code-{len(owner.codes) + 1}"
                    owner.codes[code] = query
                    callback = query["redirect_uri"] + "?" + urlencode({
                        "code": code, "state": query["state"], "iss": owner.issuer,
                    })
                    return self.send(302, headers={"Location": callback})
                if path in {"/public", "/mcp"}:
                    return self.send(405)
                return self.send(404, {"error": "not_found"})

            def do_DELETE(self):
                self.record()
                self.send(200)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                    body = {key: values[0] for key, values in parse_qs(raw.decode()).items()}
                else:
                    body = json.loads(raw) if raw else {}
                path = self.record(body).path
                if path == "/auth/register":
                    if not owner.allow_registration:
                        return self.send(404, {"error": "registration_not_supported"})
                    if owner.registration_error:
                        return self.send(owner.registration_error_status, {
                            "error": owner.registration_error_code, "error_description": owner.registration_error,
                        })
                    client_id = f"registered-client-{len(owner.clients) + 1}"
                    registration = dict(body, client_id=client_id, token_endpoint_auth_method="none")
                    if owner.registration_secret:
                        registration.update({
                            "client_secret": owner.registration_secret,
                            "token_endpoint_auth_method": "client_secret_post",
                        })
                        owner.issued_secrets.append(owner.registration_secret)
                    owner.clients[client_id] = registration
                    return self.send(201, registration)
                if path == "/auth/issue":
                    if owner.registration_secret and body.get("client_secret") != owner.registration_secret:
                        owner.violation = "The registered confidential client omitted its client secret."
                        return self.send(401, {"error": "invalid_client"})
                    if body.get("grant_type") == "authorization_code":
                        authorization = owner.codes.pop(body.get("code"), None)
                        challenge = base64.urlsafe_b64encode(
                            hashlib.sha256(body.get("code_verifier", "").encode()).digest()
                        ).decode().rstrip("=")
                        if (
                            not authorization
                            or body.get("client_id") != authorization.get("client_id")
                            or body.get("redirect_uri") != authorization.get("redirect_uri")
                            or challenge != authorization.get("code_challenge")
                            or body.get("resource") != owner.protected_url
                        ):
                            owner.violation = "The token exchange did not bind its code, PKCE, client and resource."
                            return self.send(400, {"error": "invalid_grant"})
                    elif body.get("grant_type") == "refresh_token":
                        if (
                            body.get("refresh_token") not in owner.refresh_tokens
                            or body.get("resource") != owner.protected_url
                        ):
                            return self.send(400, {"error": "invalid_grant"})
                        owner.refresh_tokens.remove(body["refresh_token"])
                    else:
                        return self.send(400, {"error": "unsupported_grant_type"})
                    sequence = owner.counts[("POST", "/auth/issue")]
                    access, refresh = f"private-access-{sequence}", f"private-refresh-{sequence}"
                    owner.tokens.add(access)
                    owner.refresh_tokens.add(refresh)
                    owner.issued_secrets.extend([access, refresh])
                    return self.send(200, {
                        "access_token": access, "refresh_token": refresh,
                        "token_type": "Bearer", "expires_in": owner.ttl,
                        "scope": "notes:read notes:write",
                    })
                if path not in {"/public", "/mcp"}:
                    return self.send(404, {"error": "not_found"})
                if path == "/mcp" and self.headers.get("Authorization", "").removeprefix("Bearer ") not in owner.tokens:
                    return self.send(401, {"error": "invalid_token"}, {
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="'
                            + owner.base + '/.well-known/oauth-protected-resource/mcp"'
                        ),
                    })
                if "id" not in body:
                    return self.send(202)
                method = body.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "wire-notes", "version": "1.0.0"},
                    }
                elif method == "tools/list":
                    rows = copy.deepcopy(owner.catalog)
                    if owner.echo_secrets:
                        for row in rows:
                            row["description"] += " Provider echoes: " + " ".join(owner.issued_secrets)
                    result = ({"tools": rows[1:]} if body.get("params", {}).get("cursor") == "page-2"
                              else {"tools": rows[:1], "nextCursor": "page-2"})
                elif method == "tools/call":
                    params = body.get("params", {})
                    name = params.get("name")
                    if name not in {row["name"] for row in owner.catalog}:
                        owner.violation = f"Namespace leaked to remote server: {name!r}"
                        return self.send(200, {
                            "jsonrpc": "2.0", "id": body["id"],
                            "error": {"code": -32602, "message": "Unknown tool"},
                        })
                    content = {"tool": name, "arguments": params.get("arguments", {})}
                    if owner.echo_authorization:
                        content["authorization"] = self.headers.get("Authorization")
                    if owner.echo_secrets:
                        content["provider_echoes"] = list(owner.issued_secrets)
                    result = {"content": [{
                        "type": "text",
                        "text": json.dumps(content),
                    }]}
                else:
                    return self.send(200, {
                        "jsonrpc": "2.0", "id": body["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    })
                return self.send(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.issuer = self.base + "/auth"
        self.protected_url = self.base + "/mcp"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def authorize(self, authorization_url):
        response = httpx.get(authorization_url, follow_redirects=False, trust_env=False)
        assert response.status_code == 302, response.text
        return {key: values[0] for key, values in parse_qs(
            urlsplit(response.headers["Location"]).query
        ).items()}

    def calls(self, method):
        return [
            entry["body"]["params"] for entry in self.requests
            if isinstance(entry["body"], dict) and entry["body"].get("method") == method
        ]


@pytest.fixture
def wire_server():
    server = WireMCP()
    try:
        yield server
        assert server.violation is None
    finally:
        server.close()


def public_connection(manager, wire_server, name="Public notes", auth="none"):
    item = manager.add({"name": name, "url": wire_server.base + "/public", "auth": auth})
    result = manager.connect(item["id"], CALLBACK)
    assert result["state"] == "connected"
    assert not result.get("authorization_url")
    return item["id"]


def authenticated_connection(manager, wire_server, **options):
    item = manager.add({
        "name": "Private notes", "url": wire_server.protected_url, "auth": "auto", **options,
    })
    result = manager.connect(item["id"], CALLBACK)
    assert result["authorization_url"]
    query = wire_server.authorize(result["authorization_url"])
    connected = manager.complete_auth(query, CALLBACK)
    assert connected["state"] == "connected"
    return item["id"], query


@pytest.mark.parametrize("auth", ["none", "auto"])
def test_public_connection_lists_paged_tools_and_calls_unprefixed_remote_name(tmp_path, wire_server, auth):
    manager = RemoteMCP(tmp_path)
    connection_id = public_connection(manager, wire_server, auth=auth)
    catalog = manager.tools(connection_id)
    assert {tool["name"] for tool in catalog["tools"]} == {connection_id + ":read_note"}
    result = manager.call(connection_id + ":read_note", {"name": "readme"})
    assert json.loads(result["result"]["content"][0]["text"]) == {
        "tool": "read_note", "arguments": {"name": "readme"},
    }
    assert result["gateway"] == wire_server.base + "/public"
    assert result["provider"] == connection_id
    assert wire_server.calls("tools/call")[-1]["name"] == "read_note"
    assert not any(request["authorization"] for request in wire_server.requests)
    assert wire_server.counts[("POST", "/auth/register")] == 0


def test_remote_tools_require_human_opt_in_for_non_readonly_calls(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    connection_id = public_connection(manager, wire_server)
    with pytest.raises(IntegrationError):
        manager.call(connection_id + ":write_note", {"text": "not authorized"})
    assert wire_server.calls("tools/call") == []
    manager.configure(connection_id, {"enabled_tools": ["read_note", "write_note"]})
    dispatch = {
        tool["name"]: tool["dispatch_tool"] for tool in manager.tools(connection_id)["tools"]
    }
    assert dispatch[connection_id + ":read_note"] == "integration_call"
    assert dispatch[connection_id + ":write_note"] == "integration_execute"
    with pytest.raises(IntegrationError):
        manager.call(connection_id + ":write_note", {"text": "wrong permission route"}, read_only=True)
    assert wire_server.calls("tools/call") == []
    result = manager.call(connection_id + ":write_note", {"text": "explicitly enabled"})
    assert not result["result"].get("isError")
    assert wire_server.calls("tools/call")[-1] == {
        "name": "write_note", "arguments": {"text": "explicitly enabled"},
    }
    with pytest.raises(IntegrationError):
        manager.call(connection_id + ":read_note", {"name": 42})
    assert len(wire_server.calls("tools/call")) == 1


def test_oauth_pkce_discovery_registration_persistence_and_callback_replay(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    connection_id, callback = authenticated_connection(manager, wire_server)
    assert wire_server.counts[("GET", "/.well-known/oauth-protected-resource/mcp")] >= 1
    assert wire_server.counts[("POST", "/auth/register")] == 1
    assert wire_server.counts[("POST", "/auth/issue")] == 1
    authorization = next(
        request["query"] for request in wire_server.requests if request["path"] == "/auth/authorize"
    )
    assert authorization["resource"] == [wire_server.protected_url]
    assert authorization["code_challenge_method"] == ["S256"]
    assert authorization["redirect_uri"] == [CALLBACK]
    assert "client_secret" not in authorization

    reopened = RemoteMCP(tmp_path)
    result = reopened.call(connection_id + ":read_note", {"name": "after reopening"})
    assert "after reopening" in result["result"]["content"][0]["text"]
    assert wire_server.counts[("POST", "/auth/register")] == 1
    assert wire_server.counts[("POST", "/auth/issue")] == 1
    public_state = json.dumps(reopened.status())
    assert "private-access" not in public_state
    assert "private-refresh" not in public_state
    private_files = [
        path for path in (tmp_path / ".integrations").rglob("*.json")
        if "private-access" in path.read_text()
    ]
    assert private_files, "OAuth state must survive a host restart."
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_files)
    with pytest.raises(IntegrationError):
        reopened.complete_auth(callback, CALLBACK)
    assert wire_server.counts[("POST", "/auth/issue")] == 1


def test_expired_access_refreshes_at_discovered_endpoint_after_reopening(tmp_path, wire_server):
    wire_server.ttl = 2
    manager = RemoteMCP(tmp_path)
    connection_id, _ = authenticated_connection(manager, wire_server)
    # A real expiry proves persistence retains absolute expiry and discovered
    # endpoints, rather than a test rewriting implementation-private storage.
    time.sleep(2.1)
    wire_server.ttl = 3600
    reopened = RemoteMCP(tmp_path)
    reopened.call(connection_id + ":read_note", {"name": "renewed"})
    exchanges = [
        request["body"] for request in wire_server.requests if request["path"] == "/auth/issue"
    ]
    assert [exchange["grant_type"] for exchange in exchanges] == ["authorization_code", "refresh_token"]
    assert exchanges[-1]["refresh_token"] == "private-refresh-1"
    assert wire_server.counts[("POST", "/token")] == 0
    assert wire_server.counts[("POST", "/auth/register")] == 1
    assert wire_server.counts[("GET", "/auth/authorize")] == 1
    assert any(request["authorization"] == "Bearer private-access-2" for request in wire_server.requests)


def test_pre_registered_public_client_connects_when_dynamic_registration_is_unavailable(tmp_path, wire_server):
    wire_server.allow_registration = False
    wire_server.clients["pre-registered-workbench"] = {
        "client_id": "pre-registered-workbench", "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
    }
    manager = RemoteMCP(tmp_path)
    connection_id, _ = authenticated_connection(
        manager, wire_server, auth="oauth", client_id="pre-registered-workbench",
    )
    assert wire_server.counts[("POST", "/auth/register")] == 0
    assert wire_server.counts[("POST", "/register")] == 0
    manager.call(connection_id + ":read_note", {"name": "owned client"})


def test_wrong_callback_state_cannot_exchange_code(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Private notes", "url": wire_server.protected_url, "auth": "auto"})
    result = manager.connect(item["id"], CALLBACK)
    callback = wire_server.authorize(result["authorization_url"])
    with pytest.raises(IntegrationError):
        manager.complete_auth(dict(callback, state="wrong-state"), CALLBACK)
    assert wire_server.counts[("POST", "/auth/issue")] == 0
    manager.complete_auth(callback, CALLBACK)
    assert wire_server.counts[("POST", "/auth/issue")] == 1


def test_disconnect_removes_credentials_and_forbids_calls_without_silent_reconnect(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    connection_id, _ = authenticated_connection(manager, wire_server)
    manager.disconnect(connection_id)
    item = next(item for item in RemoteMCP(tmp_path).status() if item["id"] == connection_id)
    assert not item["connected"]
    calls_before = len(wire_server.calls("tools/call"))
    with pytest.raises(IntegrationError):
        manager.call(connection_id + ":read_note", {"name": "after disconnect"})
    assert len(wire_server.calls("tools/call")) == calls_before
    assert wire_server.counts[("POST", "/auth/issue")] == 1
    assert not any(
        "private-access" in path.read_text() or "private-refresh" in path.read_text()
        for path in (tmp_path / ".integrations").rglob("*.json")
    )


def test_disconnect_invalidates_inflight_browser_sign_in(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Private notes", "url": wire_server.protected_url, "auth": "auto"})
    result = manager.connect(item["id"], CALLBACK)
    callback = wire_server.authorize(result["authorization_url"])
    manager.disconnect(item["id"])
    with pytest.raises(IntegrationError):
        manager.complete_auth(callback, CALLBACK)
    assert wire_server.counts[("POST", "/auth/issue")] == 0


def test_bearer_connection_stays_host_private_even_when_server_echoes_token(tmp_path, wire_server):
    wire_server.tokens.add("explicit-private-bearer")
    wire_server.echo_authorization = True
    manager = RemoteMCP(tmp_path)
    item = manager.add({
        "name": "Token notes", "url": wire_server.protected_url,
        "auth": "bearer", "bearer_token": "explicit-private-bearer",
    })
    assert manager.connect(item["id"], CALLBACK)["state"] == "connected"
    result = manager.call(item["id"] + ":read_note", {"name": "echoed authorization"})
    assert "[redacted]" in result["result"]["content"][0]["text"]
    assert "explicit-private-bearer" not in json.dumps(result)
    assert "explicit-private-bearer" not in json.dumps(manager.status())
    assert wire_server.counts[("POST", "/auth/register")] == 0
    assert wire_server.counts[("POST", "/auth/issue")] == 0
    assert any(
        request["authorization"] == "Bearer explicit-private-bearer"
        for request in wire_server.requests
    )


def test_connection_namespaces_and_tool_grants_are_independent(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    first = public_connection(manager, wire_server, "First server")
    second = public_connection(manager, wire_server, "Second server")
    assert first != second
    manager.configure(first, {"enabled_tools": ["read_note", "write_note"]})
    manager.call(first + ":write_note", {"text": "authorized for first connection"})
    with pytest.raises(IntegrationError):
        manager.call(second + ":write_note", {"text": "not authorized for second connection"})
    assert len(wire_server.calls("tools/call")) == 1
    manager.disconnect(first)
    manager.call(second + ":read_note", {"name": "still connected"})
    assert len(wire_server.calls("tools/call")) == 2


def test_user_denial_ends_sign_in_without_token_exchange(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Private notes", "url": wire_server.protected_url, "auth": "auto"})
    result = manager.connect(item["id"], CALLBACK)
    state = parse_qs(urlsplit(result["authorization_url"]).query)["state"][0]
    with pytest.raises(IntegrationError):
        manager.complete_auth({"state": state, "error": "access_denied"}, CALLBACK)
    assert wire_server.counts[("POST", "/auth/issue")] == 0
    state = next(state for state in manager.status() if state["id"] == item["id"])
    assert not state["connected"]


def test_oauth_callback_rejects_issuer_mixup_before_exchange(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Private notes", "url": wire_server.protected_url, "auth": "auto"})
    result = manager.connect(item["id"], CALLBACK)
    callback = wire_server.authorize(result["authorization_url"])
    with pytest.raises(IntegrationError):
        manager.complete_auth(dict(callback, iss="https://another-issuer.example"), CALLBACK)
    assert wire_server.counts[("POST", "/auth/issue")] == 0


def test_callback_cannot_be_redeemed_on_another_workbench_origin(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Private notes", "url": wire_server.protected_url, "auth": "auto"})
    result = manager.connect(item["id"], CALLBACK)
    callback = wire_server.authorize(result["authorization_url"])
    with pytest.raises(IntegrationError):
        manager.complete_auth(callback, "http://127.0.0.1:8766/api/integrations/mcp/callback")
    assert wire_server.counts[("POST", "/auth/issue")] == 0
    manager.complete_auth(callback, CALLBACK)
    assert wire_server.counts[("POST", "/auth/issue")] == 1


def test_auto_enabled_read_tool_becoming_write_requires_fresh_human_opt_in(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    connection_id = public_connection(manager, wire_server)
    wire_server.catalog[0]["annotations"]["readOnlyHint"] = False
    with pytest.raises(IntegrationError):
        manager.call(connection_id + ":read_note", {"name": "now changes data"}, read_only=False)
    assert not wire_server.calls("tools/call")
    manager.configure(connection_id, {"enabled_tools": ["read_note"]})
    manager.call(connection_id + ":read_note", {"name": "new permission explicitly granted"}, read_only=False)
    assert len(wire_server.calls("tools/call")) == 1


def test_new_read_tools_do_not_override_a_humans_custom_tool_selection(tmp_path, wire_server):
    manager = RemoteMCP(tmp_path)
    connection_id = public_connection(manager, wire_server)
    manager.configure(connection_id, {"enabled_tools": ["read_note"]})
    wire_server.catalog.append(dict(copy.deepcopy(READ_TOOL), name="new_read_note"))
    manager.refresh(connection_id)
    assert {row["name"] for row in manager.tools(connection_id)["tools"]} == {
        connection_id + ":read_note",
    }
    with pytest.raises(IntegrationError):
        manager.call(connection_id + ":new_read_note", {"name": "unapproved new capability"})
    assert not wire_server.calls("tools/call")


def test_client_secret_and_rotated_tokens_cannot_leak_via_remote_catalog_or_result(tmp_path, wire_server):
    wire_server.ttl = 2
    wire_server.echo_secrets = True
    wire_server.registration_secret = "private-registered-secret"
    manager = RemoteMCP(tmp_path)
    connection_id, _ = authenticated_connection(manager, wire_server)
    first_catalog = json.dumps(manager.tools(connection_id))
    assert all(secret not in first_catalog for secret in wire_server.issued_secrets)
    time.sleep(2.1)
    wire_server.ttl = 3600
    reopened = RemoteMCP(tmp_path)
    result = reopened.call(connection_id + ":read_note", {"name": "secret echoes after renewal"})
    public = json.dumps([result, reopened.tools(connection_id), reopened.status()])
    assert all(secret not in public for secret in wire_server.issued_secrets)
    assert "[redacted]" in public
    assert len(wire_server.issued_secrets) == 5  # DCR secret; initial and rotated access/refresh.


@pytest.mark.parametrize(("status", "code", "expected"), [
    (400, "invalid_client_metadata", "rejected the OAuth client metadata"),
    (403, "access_denied", "denied OAuth client registration"),
    (404, "registration_not_supported", "registration endpoint is unavailable"),
    (500, "server_error", "failed during OAuth client registration"),
])
def test_failed_dynamic_registration_does_not_expose_provider_error_body(tmp_path, wire_server,
                                                                       status, code, expected):
    wire_server.registration_error = "sensitive-upstream-detail private-registration-token"
    wire_server.registration_error_status = status
    wire_server.registration_error_code = code
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Unavailable registration", "url": wire_server.protected_url, "auth": "auto"})
    with pytest.raises(IntegrationError) as caught:
        manager.connect(item["id"], CALLBACK)
    public = str(caught.value) + json.dumps(manager.status())
    assert expected in str(caught.value)
    assert f"HTTP {status}, {code}" in str(caught.value)
    assert "requires an approved OAuth client" not in public
    assert manager.status()[0]["state"] == "error"
    assert wire_server.registration_error not in public
    assert "private-registration-token" not in public
    assert wire_server.counts[("POST", "/auth/issue")] == 0
    assert wire_server.counts[("GET", "/auth/authorize")] == 0
