"""Host storage, configuration and validation for arbitrary remote MCP peers."""

import asyncio
import json
import socket

import httpx2
import pytest
from mcp.client.auth.exceptions import OAuthRegistrationError
from mcp.client.auth.oauth2 import check_registration_usable
from mcp.client.auth.utils import handle_registration_response
from mcp.shared.auth import OAuthClientInformationFull

from vwb.integrations.remote_mcp import (
    CALLBACK_PATH,
    RemoteMCP,
    _clean_catalog,
    _redact,
    _safe_error,
    _TokenStorage,
    _url,
    _validate_destination,
)
from vwb.integrations.store import IntegrationError


@pytest.mark.parametrize("url", [
    "http://example.com/mcp", "ftp://example.com/mcp", "https://user:password@example.com/mcp",
    "https://example.com/mcp#fragment", "https://example.com/mcp?api_key=secret",
    "https://example.com/\nfoo", "https://example.com:invalid/mcp",
])
def test_invalid_server_urls(tmp_path, url):
    manager = RemoteMCP(tmp_path)
    with pytest.raises(IntegrationError):
        manager.add({"name": "Server", "url": url})


def test_public_status_never_exposes_credentials(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp",
                        "auth": "bearer", "bearer_token": "very-private-token",
                        "client_secret": "very-private-client-secret"})
    manager._update(item["id"], {"tokens": {"access_token": "private-access", "refresh_token": "private-refresh"},
                                "client_info": {"client_secret": "another-private-secret"}})
    status = json.dumps(manager.status())
    assert "private" not in status
    assert manager.store.path.stat().st_mode & 0o777 == 0o600
    assert manager.store.root.stat().st_mode & 0o777 == 0o700
    assert manager.status()[0]["id"] == item["id"]


def test_config_change_invalidates_tokens_and_old_writers(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Original", "url": "https://example.com/mcp", "auth": "oauth"})
    generation = manager._record(item["id"])["generation"]
    storage = _TokenStorage(manager, item["id"], generation)
    manager._update(item["id"], {"tokens": {"access_token": "old-token"}, "state": "connected"})
    renamed = manager.configure(item["id"], {"name": "Renamed"})
    assert renamed["state"] == "connected"
    assert storage.record()["tokens"]["access_token"] == "old-token"
    changed = manager.configure(item["id"], {"url": "https://elsewhere.example/mcp"})
    assert changed["state"] == "disconnected"
    assert manager._record(item["id"])["tokens"] is None
    with pytest.raises(IntegrationError, match="changed"):
        storage.record()
    with pytest.raises(IntegrationError, match="changed"):
        manager._update(item["id"], {"tokens": {"access_token": "late-write"}}, generation=generation)


def test_unknown_tools_cannot_be_enabled(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp"})
    with pytest.raises(IntegrationError, match="catalog"):
        manager.configure(item["id"], {"enabled_tools": ["invented"]})


def test_secret_blank_preserves_value(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp",
                        "auth": "bearer", "bearer_token": "secret"})
    manager.configure(item["id"], {"bearer_token": ""})
    assert manager._record(item["id"])["bearer_token"] == "secret"
    manager.remove(item["id"])
    assert manager.status() == []


def test_schema_external_references_are_rejected():
    with pytest.raises(IntegrationError, match="local definitions"):
        _clean_catalog([{"name": "get", "inputSchema": {
            "type": "object", "properties": {"thing": {"$ref": "https://example.com/schema"}}}}])


def test_safe_errors_do_not_include_provider_body():
    assert "private-secret" not in str(_safe_error(ValueError("registration error private-secret")))
    assert "approved" not in str(_safe_error(ValueError("registration error private-secret")))
    assert "registration" not in str(_safe_error(ValueError("registration error private-secret")))
    assert _redact({"text": "token old-token", "items": ["refresh"]},
                   ["", "old-token", "refresh"]) == {"text": "token [redacted]", "items": ["[redacted]"]}


def test_callback_requires_exact_local_route():
    assert _url("http://127.0.0.1:8765" + CALLBACK_PATH, callback=True)
    with pytest.raises(IntegrationError):
        _url("http://127.0.0.1:8765/other", callback=True)
    with pytest.raises(IntegrationError):
        _url("https://example.com" + CALLBACK_PATH, callback=True)


def test_metadata_cannot_pivot_public_server_into_local_network(monkeypatch):
    with pytest.raises(IntegrationError, match="local address"):
        asyncio.run(_validate_destination("http://localhost:8765/mcp", allow_local=False))
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(IntegrationError, match="public address"):
        asyncio.run(_validate_destination("https://private.example/mcp", allow_local=False))


def _tool(name, readonly):
    return {"name": name, "description": "", "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": readonly, "destructiveHint": False}}


def test_permission_changes_do_not_inherit_authority(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp"})
    generation = manager._record(item["id"])["generation"]
    assert manager._publish_catalog(item["id"], generation, [_tool("read", True)], []) == {"read"}
    # A server changing a formerly read-only tool into a write cannot inherit consent.
    assert manager._publish_catalog(item["id"], generation, [_tool("read", False)], []) == set()
    manager.configure(item["id"], {"enabled_tools": ["read"]})
    assert manager._publish_catalog(item["id"], generation, [_tool("read", False)], []) == {"read"}
    # After a person chooses a subset, subsequent discovery preserves that subset.
    manager.configure(item["id"], {"enabled_tools": []})
    assert manager._publish_catalog(item["id"], generation,
                                    [_tool("read", False), _tool("new-read", True)], []) == set()


def test_choosing_read_tool_does_not_approve_future_writes(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp"})
    generation = manager._record(item["id"])["generation"]
    manager._publish_catalog(item["id"], generation, [_tool("read", True)], [])
    manager.configure(item["id"], {"enabled_tools": ["read"]})
    assert manager._publish_catalog(item["id"], generation, [_tool("read", False)], []) == set()


def test_disconnected_generation_cannot_publish_catalog(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp"})
    generation = manager._record(item["id"])["generation"]
    manager.disconnect(item["id"])
    with pytest.raises(IntegrationError, match="changed"):
        manager._publish_catalog(item["id"], generation, [_tool("read", True)], [])


def test_changing_endpoint_cannot_forward_old_credentials(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp",
                        "auth": "bearer", "bearer_token": "old-server-token"})
    with pytest.raises(IntegrationError, match="bearer token"):
        manager.configure(item["id"], {"url": "https://other.example/mcp"})
    assert manager._record(item["id"])["url"] == "https://example.com/mcp"
    manager.configure(item["id"], {"url": "https://other.example/mcp", "bearer_token": "new-server-token"})
    assert manager._record(item["id"])["bearer_token"] == "new-server-token"
    oauth = manager.add({"name": "OAuth", "url": "https://example.com/mcp",
                         "auth": "oauth", "client_id": "client", "client_secret": "old-client-secret"})
    with pytest.raises(IntegrationError, match="client secret"):
        manager.configure(oauth["id"], {"url": "https://other.example/mcp", "client_secret": ""})
    manager.configure(oauth["id"], {"url": "https://other.example/mcp", "auth": "none"})
    assert manager._record(oauth["id"])["client_secret"] == ""


def test_size_and_pagination_bounds(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp"})
    manager._update(item["id"], {"state": "connected"})
    with pytest.raises(IntegrationError, match="256 KiB"):
        manager.call(item["id"] + ":read", {"text": "x" * (256 * 1024)})
    with pytest.raises(IntegrationError, match="valid JSON"):
        manager.call(item["id"] + ":read", {"value": float("nan")})
    with pytest.raises(IntegrationError, match="limit"):
        manager.tools(item["id"], limit=51)


@pytest.mark.parametrize(("status", "code", "expected"), [
    (403, "access_denied", "denied OAuth client registration"),
    (400, "invalid_client_metadata", "rejected the OAuth client metadata"),
    (400, "invalid_redirect_uri", "registered callback URL"),
    (404, "registration_not_supported", "endpoint is unavailable"),
    (429, "temporarily_unavailable", "temporarily limited"),
    (500, "server_error", "failed during OAuth client registration"),
    (400, "private-arbitrary-code", "rejected OAuth client registration"),
    (400, ["not", "a", "string"], "rejected OAuth client registration"),
])
def test_registration_http_diagnostics_are_bounded_and_truthful(status, code, expected):
    response = httpx2.Response(status, json={"error": code, "error_description": "private-provider-secret"})
    with pytest.raises(OAuthRegistrationError) as caught:
        asyncio.run(handle_registration_response(response))
    message = str(_safe_error(caught.value))
    assert expected in message
    assert f"HTTP {status}" in message
    assert "private" not in message
    assert "approved OAuth client" not in message
    if isinstance(code, str) and not code.startswith("private"):
        assert code in message


def test_registration_response_and_method_errors_do_not_imply_approval():
    with pytest.raises(OAuthRegistrationError) as malformed:
        asyncio.run(handle_registration_response(httpx2.Response(201, json={"private-secret": "invalid shape"})))
    assert "invalid OAuth client registration response" in str(_safe_error(malformed.value))
    for method, expected in [
        ("private_key_jwt", "authentication method Workbench cannot use"),
        ("client_secret_post", "without its required secret"),
    ]:
        with pytest.raises(OAuthRegistrationError) as caught:
            check_registration_usable(OAuthClientInformationFull(client_id="client",
                                                                token_endpoint_auth_method=method))
        assert expected in str(_safe_error(caught.value))
    unknown = _safe_error(OAuthRegistrationError("Unrecognized SDK detail private-provider-secret"))
    assert str(unknown).startswith("OAuth client registration failed.")
    assert "private-provider-secret" not in str(unknown)


def test_registration_error_inside_task_group_retains_safe_diagnostics():
    exc = ExceptionGroup("private-group-message", [
        ValueError("unrelated"),
        OAuthRegistrationError('Registration failed: 403 {"error":"access_denied","secret":"private"}'),
    ])
    message = str(_safe_error(exc))
    assert "HTTP 403, access_denied" in message
    assert "private" not in message


def test_legacy_registration_message_is_corrected_without_mutating_store(tmp_path):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp"})
    legacy = "This server requires an approved OAuth client. Enter its client ID in advanced settings."
    manager._update(item["id"], {"state": "error", "message": legacy})
    assert manager.status()[0]["message"].startswith("OAuth client registration failed.")
    assert manager._record(item["id"])["message"] == legacy


def test_failed_registration_updates_existing_connection_state(tmp_path, monkeypatch):
    manager = RemoteMCP(tmp_path)
    item = manager.add({"name": "Server", "url": "https://example.com/mcp"})
    manager._update(item["id"], {"state": "connected"})

    async def failed_registration(*args, **kwargs):
        raise OAuthRegistrationError('Registration failed: 400 {"error":"invalid_client_metadata"}')

    monkeypatch.setattr(manager, "_operation", failed_registration)
    with pytest.raises(IntegrationError, match="HTTP 400"):
        manager.refresh(item["id"])
    assert manager.status()[0]["state"] == "error"
    assert "invalid_client_metadata" in manager.status()[0]["message"]

    async def bad_arguments(*args, **kwargs):
        raise IntegrationError("Arguments do not match the MCP tool's input schema.")

    manager._update(item["id"], {"state": "connected"})
    monkeypatch.setattr(manager, "_operation", bad_arguments)
    with pytest.raises(IntegrationError, match="Arguments"):
        manager._run(item["id"], "call")
    assert manager.status()[0]["state"] == "connected"
