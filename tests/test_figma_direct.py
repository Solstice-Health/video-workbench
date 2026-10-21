"""Direct Figma auth, read tools and artifact paths, without external accounts."""
from __future__ import annotations

import base64
import io
import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from vwb.integrations import IntegrationError, IntegrationService
from vwb.integrations.figma_direct import API, CALLBACK_PATH, DirectFigma
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace
from vwb.workspace.references import validate

CALLBACK = "http://127.0.0.1:8765" + CALLBACK_PATH


@pytest.fixture
def direct(tmp_path):
    calls = []
    picture = io.BytesIO()
    Image.new("RGB", (8, 8), "orange").save(picture, format="PNG")
    responses = {"file_status": 200, "cdn_url": "https://s3-alpha.figma.com/test.png", "image": picture.getvalue(),
                 "mime": "image/png", "token_status": 200, "me_status": 200}

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path in {"/v1/oauth/token", "/v1/oauth/refresh"}:
            value = {"access_token": "secret-access", "expires_in": 3600, "token_type": "bearer"}
            if path.endswith("/token"):
                value["refresh_token"] = "secret-refresh"
            else:
                value["access_token"] = responses.get("refresh_access", "secret-access")
            return httpx.Response(responses["token_status"], json=value)
        if path == "/v1/me":
            if responses.get("me_sequence"):
                status, body = responses["me_sequence"].pop(0)
                return httpx.Response(status, json=body)
            return httpx.Response(responses["me_status"], json=responses.get("me_body", {"id": "user123"}))
        if path.startswith("/v1/files/"):
            if "file_raw" in responses:
                return httpx.Response(responses["file_status"], content=responses["file_raw"])
            if responses.get("file_sequence"):
                status, body = responses["file_sequence"].pop(0)
                return httpx.Response(status, json=body)
            return httpx.Response(responses["file_status"], headers={"Retry-After": "15"},
                                  json=responses.get("file_body", {"name": "A design", "document": {"id": "0:0"},
                                                                  "echo": "secret-access secret-client"}))
        if path.startswith("/v1/images/"):
            return httpx.Response(200, json={"images": {"1:2": responses["cdn_url"], "1:3": None}})
        return httpx.Response(200, content=responses["image"], headers={"Content-Type": responses["mime"]})

    service = DirectFigma(tmp_path / "host", client=httpx.Client(transport=httpx.MockTransport(handler)))
    service.configure({"client_id": "direct-client", "client_secret": "secret-client", "redirect_uri": CALLBACK})
    service.calls = calls
    service.responses = responses
    return service


def login(direct):
    auth = direct.begin_auth(CALLBACK)
    query = parse_qs(urlsplit(auth["authorization_url"]).query)
    direct.complete_auth("one-use-code", query["state"][0], CALLBACK)
    return query


def workspace(tmp_path):
    ws = Workspace(tmp_path / "r-direct")
    ws.work.mkdir(parents=True)
    return ws


def test_direct_oauth_pkce_basic_auth_persistence_and_no_secrets_in_status(direct):
    query = login(direct)
    assert query["scope"] == ["file_content:read current_user:read"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [CALLBACK]
    assert "audience" not in query and "secret" not in direct.begin_auth(CALLBACK)["authorization_url"]
    request = direct.calls[0]
    assert str(request.url) == API + "/v1/oauth/token"
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"direct-client:secret-client").decode()
    form = parse_qs(request.content.decode())
    assert form["code_verifier"] and form["grant_type"] == ["authorization_code"]
    reopened = DirectFigma(direct.store.root.parent.parent, client=direct.client)
    assert reopened.status()["item"]["connected"] and reopened.status()["item"]["persistent"]
    assert "secret-" not in json.dumps(reopened.status())
    assert stat.S_IMODE(direct.store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(direct.store.root.stat().st_mode) == 0o700


def test_state_mismatch_expiry_replay_and_callback_binding(direct):
    query = parse_qs(urlsplit(direct.begin_auth(CALLBACK)["authorization_url"]).query)
    nonce = query["state"][0]
    for state, callback in [("wrong", CALLBACK), (nonce, CALLBACK.replace("127.0.0.1", "localhost"))]:
        with pytest.raises(IntegrationError, match="state"):
            direct.complete_auth("code", state, callback)
    assert not direct.calls
    direct.complete_auth("code", nonce, CALLBACK)
    with pytest.raises(IntegrationError, match="state"):
        direct.complete_auth("code", nonce, CALLBACK)
    direct.begin_auth(CALLBACK)
    saved = direct.store.read()
    saved["pending"]["expires_at"] = time.time() - 1
    direct.store.write(saved)
    with pytest.raises(IntegrationError, match="state"):
        direct.complete_auth("code", saved["pending"]["state"], CALLBACK)


@pytest.mark.parametrize("url", ["https://evil.example/callback", CALLBACK + "?x=1", CALLBACK + "#x",
                                CALLBACK.replace("127.0.0.1", "127.0.0.1.evil.example"),
                                CALLBACK.replace("127.0.0.1", "user@127.0.0.1"), "http://127.0.0.1:8765/other"])
def test_only_local_callback_accepted(direct, url):
    with pytest.raises(IntegrationError):
        direct.configure({"redirect_uri": url})


def test_config_change_clears_grant_secret_kept_when_blank_and_disconnect_independent(direct):
    login(direct)
    direct.configure({"client_secret": ""})
    assert direct.status()["item"]["connected"]
    direct.configure({"redirect_uri": CALLBACK.replace("8765", "8766")})
    assert not direct.status()["item"]["connected"]
    assert "pending" not in direct.store.read() and "credentials" not in direct.store.read()
    service = IntegrationService(direct.store.root.parent.parent, direct=direct)
    service.configure({"client_id": "solstice-client"})
    direct.disconnect()
    assert service.status()["config"]["client_id"] == "solstice-client"
    assert direct.status()["config"]["client_secret_configured"]


def test_refresh_retains_refresh_token_and_serializes_across_instances(direct):
    login(direct)
    saved = direct.store.read()
    saved["credentials"]["expires_at"] = 0
    direct.store.write(saved)
    reopened = DirectFigma(direct.store.root.parent.parent, client=direct.client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda service: service.verify(), [direct, reopened]))
    refreshes = [call for call in direct.calls if call.url.path == "/v1/oauth/refresh"]
    assert len(refreshes) == 1
    assert parse_qs(refreshes[0].content.decode()) == {"refresh_token": ["secret-refresh"]}
    assert direct.store.read()["credentials"]["refresh_token"] == "secret-refresh"


def test_failed_refresh_and_failed_exchange_require_reconnect(direct):
    login(direct)
    saved = direct.store.read()
    saved["credentials"]["expires_at"] = 0
    direct.store.write(saved)
    direct.responses["token_status"] = 400
    with pytest.raises(IntegrationError, match="reconnect"):
        direct.verify()
    assert direct.status()["item"]["state"] == "error"
    nonce = parse_qs(urlsplit(direct.begin_auth(CALLBACK)["authorization_url"]).query)["state"][0]
    with pytest.raises(IntegrationError):
        direct.complete_auth("bad-code", nonce, CALLBACK)
    assert "pending" not in direct.store.read()
    with pytest.raises(IntegrationError, match="state"):
        direct.complete_auth("bad-code", nonce, CALLBACK)


def test_direct_registry_call_without_solstice_imports_and_logs_and_obeys_policy(direct, tmp_path, monkeypatch):
    login(direct)
    service = IntegrationService(tmp_path / "host", direct=direct)
    assert not service.status()["items"][0]["connected"]
    assert service.tools(integration="figma_direct")["total"] == 2
    ws = workspace(tmp_path)
    ctx = ToolContext(ws, "job_lead", "job_lead", None, state={"integration_service": service})
    registry = build_registry()
    result = registry.call(ctx, "integration_call", {"tool": "figma_direct_get_file", "arguments": {"file_key": "file123", "node_ids": ["1:2"]}})
    assert not result.is_error
    assert "secret-access" not in result.text and "secret-client" not in result.text
    imported = list((ws.work / "integrations" / "figma_direct").glob("*/response.json"))
    assert len(imported) == 1 and "secret-access" not in imported[0].read_text()
    assert '"integration": "figma_direct"' in (ws.ledger / "ledger.jsonl").read_text()
    assert "integration_call" in (ws.ledger / "tools.jsonl").read_text()
    request = next(call for call in direct.calls if call.url.path.startswith("/v1/files/"))
    assert dict(request.url.params) == {"depth": "2", "ids": "1:2"}
    assert validate({"plugins": ["figma_direct"]})["plugins"] == ["figma_direct"]
    monkeypatch.setattr(registry, "mode", lambda capability: "block")
    assert registry.call(ctx, "integration_call", {"tool": "figma_direct_get_file", "arguments": {"file_key": "file123"}}).is_error


def test_images_import_verified_pixels_no_auth_to_cdn_and_missing_node_explicit(direct, tmp_path):
    login(direct)
    ws = workspace(tmp_path)
    result = direct.call(ws, "figma_direct_get_images", {"file_key": "file123", "node_ids": ["1:2", "1:3"]})
    assert len(result["images"]) == 1 and len(result["artifacts"]) == 2
    assert result["structuredContent"]["images"][1]["missing"]
    cdn = direct.calls[-1]
    assert cdn.url.host == "s3-alpha.figma.com" and "authorization" not in cdn.headers
    assert "s3-alpha.figma.com" not in json.dumps({k: v for k, v in result.items() if k != "images"})


@pytest.mark.parametrize("url", ["http://s3-alpha.figma.com/test", "https://127.0.0.1/test",
                                "https://s3-alpha.figma.com.evil.example/test", "https://user:pass@s3-alpha.figma.com/test",
                                "https://s3-alpha.figma.com:444/test", "https://evil.example/test"])
def test_preview_urls_fail_closed_before_download(direct, tmp_path, url):
    login(direct)
    direct.responses["cdn_url"] = url
    count = len(direct.calls)
    with pytest.raises(IntegrationError, match="unsupported preview address"):
        direct.call(workspace(tmp_path), "figma_direct_get_images", {"file_key": "file123", "node_ids": ["1:2"]})
    assert len(direct.calls) == count + 1


@pytest.mark.parametrize("status,message", [(401, "Reconnect"), (403, "denied"), (404, "find"), (429, "15 seconds"), (503, "503")])
def test_read_errors_are_actionable_and_never_echo_provider_body(direct, tmp_path, status, message):
    login(direct)
    direct.responses["file_status"] = status
    with pytest.raises(IntegrationError, match=message):
        direct.call(workspace(tmp_path), "figma_direct_get_file", {"file_key": "file123"})
    if status == 401:
        assert not direct.status()["item"]["connected"]


def test_schema_validation_happens_before_network_and_mutation_tools_do_not_exist(direct, tmp_path):
    login(direct)
    ws = workspace(tmp_path)
    count = len(direct.calls)
    for tool, arguments in [("figma_direct_delete_file", {}), ("figma_direct_get_file", {"file_key": "../me"}),
                            ("figma_direct_get_images", {"file_key": "file123", "node_ids": ["1:2"], "scale": 100}),
                            ("figma_direct_get_file", {"file_key": "file123", "access_token": "injected"})]:
        with pytest.raises(IntegrationError):
            direct.call(ws, tool, arguments)
    assert len(direct.calls) == count


def test_malformed_images_and_oversize_responses_not_imported(direct, tmp_path):
    login(direct)
    ws = workspace(tmp_path)
    direct.responses["image"] = b"not a PNG"
    with pytest.raises(IntegrationError, match="invalid"):
        direct.call(ws, "figma_direct_get_images", {"file_key": "file123", "node_ids": ["1:2"]})
    direct.responses["image"] = b"x" * (4 * 1024 * 1024 + 1)
    with pytest.raises(IntegrationError, match="too large"):
        direct.call(ws, "figma_direct_get_images", {"file_key": "file123", "node_ids": ["1:2"]})
    assert not (ws.work / "integrations").exists()


def test_svg_import_is_inert_and_cdn_redirects_are_not_followed(direct, tmp_path):
    login(direct)
    ws = workspace(tmp_path)
    direct.responses.update(image=b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                            mime="image/svg+xml")
    result = direct.call(ws, "figma_direct_get_images", {"file_key": "file123", "node_ids": ["1:2"], "format": "svg"})
    assert not result["images"]
    assert any(row["path"].endswith(".svg.txt") for row in result["artifacts"])
    seen = []

    def redirect(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    direct.client = httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True)
    status, _, _ = direct._request("GET", "https://s3-alpha.figma.com/preview")
    assert status == 302 and len(seen) == 1


def test_invalid_callback_text_is_safe_and_new_app_requires_its_secret(direct):
    with pytest.raises(IntegrationError, match="not completed"):
        direct.complete_auth("code", "non-ascii-\u2603", CALLBACK)
    with pytest.raises(IntegrationError, match="new Figma app"):
        direct.configure({"client_id": "other-app", "client_secret": ""})


def test_verified_account_is_bounded_allowlisted_and_survives_reopen(direct):
    direct.responses["me_body"] = {"id": "1640059738232610199", "handle": "Example Person", "email": "person@example.com",
                                   "img_url": "https://example.com/photo", "extra": "secret-access"}
    login(direct)
    reopened = DirectFigma(direct.store.root.parent.parent, client=direct.client)
    account = {"id": "1640059738232610199", "handle": "Example Person", "email": "person@example.com"}
    assert reopened.status()["item"]["account"] == account == reopened.store.read()["account"]
    assert "as Example Person" in reopened.status()["item"]["message"]
    assert "each file" in reopened.status()["item"]["message"]
    direct.responses["me_body"] = {"id": "user123", "handle": "x" * 201, "email": "secret-access@example.com"}
    direct.verify()
    assert direct.status()["item"]["account"] == {"id": "user123"}
    direct.disconnect()
    assert direct.status()["item"]["account"] == {}


@pytest.mark.parametrize("body", [{"status": 403, "err": "Request denied"}, {"err": "Unrecognized upstream failure"},
                                 {"err": "Invalid token secret-access secret-client"},
                                 {"error": {"message": "secret-refresh"}}, {"err": "x" * 5000}, "not an error object"])
def test_generic_403_preserves_grant_does_not_refresh_and_never_echoes_body(direct, tmp_path, body):
    login(direct)
    before = direct.store.read()
    direct.responses.update(file_status=403, file_body=body)
    with pytest.raises(IntegrationError, match="HTTP 403") as caught:
        direct.call(workspace(tmp_path), "figma_direct_get_file", {"file_key": "file123"})
    assert "does not prove the sign-in expired" in str(caught.value)
    assert "reconnect" not in str(caught.value).lower()
    assert "secret-" not in str(caught.value)
    assert not any(call.url.path == "/v1/oauth/refresh" for call in direct.calls)
    assert direct.store.read() == before
    assert direct.status()["item"]["connected"]


@pytest.mark.parametrize("body", [{"err": "Invalid scope"}, {"message": "Invalid scope(s): file_content:read"},
                                 {"error": {"code": "insufficient_scope", "message": "secret-access"}}])
def test_missing_scope_reports_scope_action_without_refresh_or_credential_loss(direct, tmp_path, body):
    login(direct)
    before = direct.store.read()
    direct.responses.update(file_status=403, file_body=body)
    with pytest.raises(IntegrationError, match="read scope.*HTTP 403") as caught:
        direct.call(workspace(tmp_path), "figma_direct_get_file", {"file_key": "file123"})
    assert "file_content:read" in str(caught.value) and "secret-" not in str(caught.value)
    assert not any(call.url.path == "/v1/oauth/refresh" for call in direct.calls)
    assert direct.store.read() == before


@pytest.mark.parametrize("status,body", [(403, {"err": "Invalid token"}), (403, {"error": "expired_token"}),
                                       (401, {"err": "secret-access"})])
def test_rejected_credential_refreshes_once_and_success_redacts_old_and_new_tokens(direct, tmp_path, status, body):
    login(direct)
    direct.responses.update(refresh_access="replacement-access", file_sequence=[(status, body),
                            (200, {"name": "Design", "echo": "secret-access replacement-access secret-client secret-refresh"})])
    ws = workspace(tmp_path)
    result = direct.call(ws, "figma_direct_get_file", {"file_key": "file123"})
    assert len([call for call in direct.calls if call.url.path == "/v1/oauth/refresh"]) == 1
    reads = [call for call in direct.calls if call.url.path.startswith("/v1/files/")]
    assert len(reads) == 2
    assert reads[0].headers["Authorization"] == "Bearer secret-access"
    assert reads[1].headers["Authorization"] == "Bearer replacement-access"
    imported = next((ws.work / "integrations" / "figma_direct").glob("*/response.json")).read_text()
    for secret in ("secret-access", "replacement-access", "secret-client", "secret-refresh"):
        assert secret not in json.dumps(result) and secret not in imported
    assert direct.status()["item"]["connected"]


@pytest.mark.parametrize("status,body", [(403, {"err": "Access token expired"}), (401, {"error": "invalid_token"})])
def test_repeated_credential_rejection_stops_after_one_refresh(direct, tmp_path, status, body):
    login(direct)
    direct.responses.update(file_status=status, file_body=body)
    with pytest.raises(IntegrationError, match="Reconnect Figma direct"):
        direct.call(workspace(tmp_path), "figma_direct_get_file", {"file_key": "file123"})
    assert len([call for call in direct.calls if call.url.path == "/v1/oauth/refresh"]) == 1
    assert len([call for call in direct.calls if call.url.path.startswith("/v1/files/")]) == 2
    assert not direct.status()["item"]["connected"]


def test_proactive_refresh_counts_toward_single_retry_budget(direct, tmp_path):
    login(direct)
    saved = direct.store.read()
    saved["credentials"]["expires_at"] = 0
    direct.store.write(saved)
    direct.responses.update(file_status=403, file_body={"err": "Invalid token"})
    with pytest.raises(IntegrationError, match="Reconnect Figma direct"):
        direct.call(workspace(tmp_path), "figma_direct_get_file", {"file_key": "file123"})
    assert len([call for call in direct.calls if call.url.path == "/v1/oauth/refresh"]) == 1
    assert len([call for call in direct.calls if call.url.path.startswith("/v1/files/")]) == 1


def test_identity_after_refresh_drops_both_old_and_new_reflected_credentials(direct):
    login(direct)
    direct.responses.update(refresh_access="replacement-access", me_sequence=[(403, {"err": "Invalid token"}),
                            (200, {"id": "user123", "handle": "secret-access", "email": "replacement-access@example.com"})])
    direct.verify()
    assert direct.status()["item"]["account"] == {"id": "user123"}
    assert direct.store.read()["account"] == {"id": "user123"}


@pytest.mark.parametrize("raw", [b"secret-access is not JSON", b"[" * 2000 + b"]" * 2000])
def test_malformed_error_body_remains_generic_and_does_not_refresh(direct, tmp_path, raw):
    login(direct)
    direct.responses.update(file_status=403, file_raw=raw)
    with pytest.raises(IntegrationError, match="HTTP 403") as caught:
        direct.call(workspace(tmp_path), "figma_direct_get_file", {"file_key": "file123"})
    assert "secret-access" not in str(caught.value)
    assert not any(call.url.path == "/v1/oauth/refresh" for call in direct.calls)
    assert direct.status()["item"]["connected"]
