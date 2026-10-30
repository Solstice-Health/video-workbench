"""Pinned public OAuth callbacks; no external accounts, DNS or paid calls."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_integrations import FakeHTTP, FakeMCP
from test_remote_mcp_protocol import WireMCP

from vwb.integrations import IntegrationError, IntegrationService, figma_direct, remote_mcp, service
from vwb.integrations.callbacks import public_origin, validate_callback

ORIGIN = "https://workbench.example"
VALIDATORS = [
    (service._callback, service.CALLBACK_PATH),
    (figma_direct._callback, figma_direct.CALLBACK_PATH),
    (lambda value: remote_mcp._url(value, callback=True), remote_mcp.CALLBACK_PATH),
]


@pytest.fixture(autouse=True)
def isolated_origin(monkeypatch, tmp_path):
    monkeypatch.delenv("VWB_UI_PUBLIC_URL", raising=False)
    monkeypatch.setenv("VWB_ENV_FILE", str(tmp_path / "empty-test.env"))


def test_public_origin_disabled_explicit_and_environment(monkeypatch):
    assert public_origin() == ""
    assert public_origin(None) == ""
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", "HTTPS://Workbench.Example:443")
    assert public_origin() == ORIGIN
    assert public_origin(None) == ORIGIN
    assert public_origin("") == ""
    assert public_origin("https://Other.Example:8443") == "https://other.example:8443"
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", "invalid-environment-origin")
    assert public_origin("") == ""
    with pytest.raises(IntegrationError, match="VWB_UI_PUBLIC_URL"):
        public_origin(None)


@pytest.mark.parametrize("value", [
    "http://workbench.example", "//workbench.example", "https:///workbench.example",
    "https://workbench.example/", "https://workbench.example/path", "https://workbench.example?",
    "https://workbench.example?q=x", "https://workbench.example#", "https://workbench.example#fragment",
    "https://private-user:private-password@workbench.example", "https://@workbench.example",
    "https://workbench.example@elsewhere.example", "https://workbench.example%40elsewhere.example",
    "https://workbench.example%2felsewhere.example", "https://workbench.example\\elsewhere.example",
    "https://workbench.example,elsewhere.example", "https://workbench.example:443:444",
    "https://workbench.example:", "https://workbench.example:0", "https://workbench.example:65536",
    "https://workbench.example:port", "https://workbench.example:443/../",
    "https://localhost", "https://dev.localhost", "https://dev.local", "https://dev.localdomain",
    "https://dev.internal", "https://dev.lan", "https://dev.home", "https://dev.home.arpa",
    "https://127.0.0.1", "https://8.8.8.8", "https://[::1]", "https://[2001:4860:4860::8888]",
    "https://127.1", "https://2130706433", "https://0x7f.0x00.0x00.0x01", "https://0177.0.0.1",
    "https://*.example", "https://workbench", "https://workbench.example.", "https://workbench..example",
    "https://-workbench.example", "https://workbench-.example", "https://work_bench.example",
    "https://" + "a" * 64 + ".example", "https://" + ("a" * 60 + ".") * 5 + "example",
    " https://workbench.example", "https://workbench.example\n", "https://workbench.ex\tample",
    "https://workbench.example\x7f", "https://workbench.éxample", "https://workbench．example",
    "https://[::1]workbench.example", "https://workbench.example:443@elsewhere.example",
    " ", 42, [], b"https://workbench.example",
])
def test_malformed_public_origins_are_rejected_without_reflecting_values(value, monkeypatch):
    with pytest.raises(IntegrationError, match="VWB_UI_PUBLIC_URL") as caught:
        public_origin(value)
    assert "private-password" not in str(caught.value)
    if isinstance(value, str):
        monkeypatch.setenv("VWB_UI_PUBLIC_URL", value)
        with pytest.raises(IntegrationError, match="VWB_UI_PUBLIC_URL"):
            public_origin()


@pytest.mark.parametrize("validate,path", VALIDATORS)
def test_public_callback_requires_host_configuration_and_exact_origin(validate, path, monkeypatch):
    with pytest.raises(IntegrationError):
        validate(ORIGIN + path)
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
    assert validate(ORIGIN + path) == ORIGIN + path
    for other in ["https://other.example", "https://workbench.example.evil.example", ORIGIN + ":8443",
                  "https://WORKBENCH.example", ORIGIN + ":443", "http://workbench.example"]:
        with pytest.raises(IntegrationError):
            validate(other + path)
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN + ":8443")
    assert validate(ORIGIN + ":8443" + path) == ORIGIN + ":8443" + path
    with pytest.raises(IntegrationError):
        validate(ORIGIN + path)


@pytest.mark.parametrize("validate,path", VALIDATORS)
def test_callback_rejects_ambiguous_shapes_even_with_configured_origin(validate, path, monkeypatch):
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
    for value in [
        ORIGIN + path + "?", ORIGIN + path + "?code=private-code", ORIGIN + path + "#",
        ORIGIN + path + "/", ORIGIN + "/other", ORIGIN + "/../" + path.lstrip("/"),
        ORIGIN + path.replace("/api/", "/%61pi/"), ORIGIN + "\\" + path,
        "https://@workbench.example" + path, "https://workbench.example%40evil.example" + path,
        "https://workbench.example@evil.example" + path, "https://workbench.example:443:444" + path,
        "http://@localhost:8765" + path, "http://localhost:8765\\@evil.example" + path,
        "http://localhost%2e:8765" + path, "http://localhost.evil.example:8765" + path,
        "http://[::1]evil.example:8765" + path, "http://localhost:" + path,
        "http://localhost:0" + path, "http://localhost:65536" + path,
        "http://localhost:8765" + path + "?", "http://localhost:8765" + path + "#",
        "\n" + ORIGIN + path, ORIGIN + path + "\n", None, 42, b"callback", "",
    ]:
        with pytest.raises(IntegrationError):
            validate(value)


@pytest.mark.parametrize("validate,path", VALIDATORS)
@pytest.mark.parametrize("origin", ["http://localhost:8765", "http://127.0.0.1:8765", "http://[::1]:8765"])
def test_existing_loopback_callbacks_remain_valid(validate, path, origin, monkeypatch):
    assert validate(origin + path) == origin + path
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
    assert validate(origin + path) == origin + path


def test_legacy_loopback_port_requirements():
    for origin in ["http://localhost", "http://127.0.0.1", "http://[::1]"]:
        assert service._callback(origin + service.CALLBACK_PATH)
        assert figma_direct._callback(origin + figma_direct.CALLBACK_PATH)
        with pytest.raises(IntegrationError):
            remote_mcp._url(origin + remote_mcp.CALLBACK_PATH, callback=True)


def test_invalid_host_configuration_fails_closed_for_all_callbacks(monkeypatch):
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN + "/")
    with pytest.raises(IntegrationError, match="VWB_UI_PUBLIC_URL"):
        validate_callback("http://localhost:8765" + service.CALLBACK_PATH, service.CALLBACK_PATH)


def test_remote_noncallback_url_rules_are_independent_of_public_origin(monkeypatch):
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", "invalid-host-config")
    for value in ["https://other.example/mcp?region=us", "http://localhost:8080/mcp",
                  "http://[::1]/mcp", "https://other.example" + remote_mcp.CALLBACK_PATH]:
        assert remote_mcp._url(value) == value
    for value in ["https://other.example:65536/mcp", "http://other.example/mcp",
                  "https://other.example/mcp?api_key=secret"]:
        with pytest.raises(IntegrationError):
            remote_mcp._url(value)


def test_solstice_https_resume_retains_exact_registration_state_and_pkce(tmp_path, monkeypatch):
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
    callback = ORIGIN + service.CALLBACK_PATH
    http, mcp = FakeHTTP(), FakeMCP()
    connector = IntegrationService(tmp_path, http=http, mcp=mcp)
    connector.configure({"client_id": "public-client", "redirect_uri": callback})
    with pytest.raises(IntegrationError, match="does not match"):
        connector.begin_auth("http://localhost:8765" + service.CALLBACK_PATH)
    query = parse_qs(urlsplit(connector.begin_auth(callback)["authorization_url"]).query)
    assert query["redirect_uri"] == [callback]
    assert query["code_challenge_method"] == ["S256"]
    reopened = IntegrationService(tmp_path, http=http, mcp=mcp)
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", "https://new-workbench.example")
    with pytest.raises(IntegrationError):
        reopened.complete_auth("code", query["state"][0], callback)
    assert not http.calls
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
    with pytest.raises(IntegrationError, match="state"):
        reopened.complete_auth("code", "wrong-state", callback)
    assert not http.calls
    result = reopened.complete_auth("code", query["state"][0], callback)
    assert result["items"][0]["connected"]
    assert http.calls[0][2]["form"]["redirect_uri"] == callback
    assert http.calls[0][2]["form"]["code_verifier"]
    with pytest.raises(IntegrationError, match="state"):
        reopened.complete_auth("code", query["state"][0], callback)
    assert len(http.calls) == 1


def test_figma_https_resume_retains_exact_registration_and_one_use_state(tmp_path, monkeypatch):
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
    callback = ORIGIN + figma_direct.CALLBACK_PATH
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access", "expires_in": 3600,
                                            "refresh_token": "test-refresh"})
        assert request.url.path == "/v1/me"
        return httpx.Response(200, json={"id": "user123"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        connector = figma_direct.DirectFigma(tmp_path, client=client)
        connector.configure({"client_id": "figma-app", "client_secret": "test-secret", "redirect_uri": callback})
        with pytest.raises(IntegrationError, match="matching"):
            connector.begin_auth("http://localhost:8765" + figma_direct.CALLBACK_PATH)
        query = parse_qs(urlsplit(connector.begin_auth(callback)["authorization_url"]).query)
        assert query["redirect_uri"] == [callback]
        assert query["code_challenge_method"] == ["S256"]
        reopened = figma_direct.DirectFigma(tmp_path, client=client)
        monkeypatch.delenv("VWB_UI_PUBLIC_URL")
        with pytest.raises(IntegrationError):
            reopened.complete_auth("code", query["state"][0], callback)
        monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
        with pytest.raises(IntegrationError, match="state"):
            reopened.complete_auth("code", query["state"][0], "http://localhost:8765" + figma_direct.CALLBACK_PATH)
        assert not calls
        assert reopened.complete_auth("code", query["state"][0], callback)["item"]["connected"]
        form = parse_qs(calls[0].content.decode())
        assert form["redirect_uri"] == [callback]
        assert form["code_verifier"]
        with pytest.raises(IntegrationError, match="state"):
            reopened.complete_auth("code", query["state"][0], callback)
        assert len(calls) == 2


@pytest.mark.parametrize("pre_registered", [False, True])
def test_remote_mcp_sdk_https_registration_resume_and_origin_binding(tmp_path, monkeypatch, pre_registered):
    """Real MCP SDK + local wire peer; the public callback itself is never fetched."""
    monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
    callback = ORIGIN + remote_mcp.CALLBACK_PATH
    server = WireMCP()
    manager = remote_mcp.RemoteMCP(tmp_path)
    item = None
    try:
        options = {}
        if pre_registered:
            server.allow_registration = False
            server.clients["registered-workbench"] = {
                "client_id": "registered-workbench", "redirect_uris": [callback],
                "token_endpoint_auth_method": "none",
            }
            options["client_id"] = "registered-workbench"
        item = manager.add({"name": "Local test MCP", "url": server.protected_url, "auth": "oauth", **options})
        result = manager.connect(item["id"], callback)
        authorization = parse_qs(urlsplit(result["authorization_url"]).query)
        assert authorization["redirect_uri"] == [callback]
        assert authorization["code_challenge_method"] == ["S256"]
        returned = server.authorize(result["authorization_url"])
        with pytest.raises(IntegrationError):
            manager.complete_auth(returned, "http://127.0.0.1:8765" + remote_mcp.CALLBACK_PATH)
        monkeypatch.setenv("VWB_UI_PUBLIC_URL", "https://different-workbench.example")
        with pytest.raises(IntegrationError):
            manager.complete_auth(returned, callback)
        assert server.counts[("POST", "/auth/issue")] == 0
        monkeypatch.setenv("VWB_UI_PUBLIC_URL", ORIGIN)
        assert manager.complete_auth(returned, callback)["state"] == "connected"
        assert server.counts[("POST", "/auth/register")] == (0 if pre_registered else 1)
        exchange = next(row["body"] for row in server.requests if row["path"] == "/auth/issue")
        assert exchange["redirect_uri"] == callback
        assert exchange["code_verifier"]
        assert all(client["redirect_uris"] == [callback] for client in server.clients.values())
        assert manager._record(item["id"])["redirect_uri"] == callback
        reopened = remote_mcp.RemoteMCP(tmp_path)
        result = reopened.call(item["id"] + ":read_note", {"name": "public callback test"})
        assert "public callback test" in result["result"]["content"][0]["text"]
        with pytest.raises(IntegrationError):
            reopened.complete_auth(returned, callback)
        assert server.counts[("POST", "/auth/issue")] == 1
        assert server.violation is None
    finally:
        if item:
            manager.disconnect(item["id"])
        server.close()
