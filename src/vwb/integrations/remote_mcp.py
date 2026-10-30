"""Direct remote MCP connections, independent of the Solstice gateway.

The SDK owns OAuth discovery, PKCE, registration and token renewal. Host-only
storage binds credentials to one connection generation. Interactive authorization
runs in a bounded background thread; a validated callback resumes that exact flow.
Agent operations never start a browser flow or grant new tool permissions.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import ipaddress
import json
import logging
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import jsonschema
from filelock import FileLock, Timeout
from mcp import ClientSession, types
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from .callbacks import validate_callback
from .store import IntegrationError, Store

CALLBACK_PATH = "/api/integrations/mcp/callback"
MAX_BYTES = 16 * 1024 * 1024
MAX_TOOLS = 500
AUTH_SECONDS = 600
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
CONNECTION_ID = re.compile(r"^mcp_[0-9a-f]{12}$")
_jobs: dict[tuple[str, str], _ConnectJob] = {}
_jobs_lock = threading.RLock()
_private_flow = contextvars.ContextVar("vwb_remote_mcp_private_flow", default=False)
_REGISTRATION_FAILURE = (
    "OAuth client registration failed. Check the server's OAuth setup or configure a registered client ID."
)
_LEGACY_REGISTRATION_MESSAGE = (
    "This server requires an approved OAuth client. Enter its client ID in advanced settings."
)
_REGISTRATION_ERROR_CODES = {
    "access_denied", "unauthorized_client", "invalid_client", "invalid_client_metadata",
    "invalid_redirect_uri", "invalid_software_statement", "unapproved_software_statement",
    "invalid_request", "registration_not_supported", "temporarily_unavailable", "server_error",
}


class _SafeAuthLogs(logging.Filter):
    """SDK protocol dumps and exception messages can contain remote secrets."""

    def filter(self, record):
        if not _private_flow.get():
            return True
        if record.levelno >= logging.WARNING:
            record.msg, record.args = "Remote MCP transport or authorization request failed.", ()
            record.exc_info = record.exc_text = record.stack_info = None
            return True
        return False


for _logger_name in ("mcp.client.auth.oauth2", "mcp.client.streamable_http", "mcp.client.session"):
    logging.getLogger(_logger_name).addFilter(_SafeAuthLogs())


def _url(value: str, *, callback=False) -> str:
    if callback:
        return validate_callback(value, CALLBACK_PATH, require_loopback_port=True)
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise IntegrationError("Enter a valid MCP server URL.")
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Validate malformed/out-of-range ports even for server URLs.
    except ValueError:
        raise IntegrationError("Enter a valid MCP server URL.") from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.fragment or any(ord(char) < 33 for char in value)):
        raise IntegrationError("Server URLs cannot contain credentials, fragments or whitespace.")
    loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise IntegrationError("Use HTTPS for remote servers, or HTTP on localhost for a local server.")
    if any(re.search(r"(?:token|secret|password|api[_-]?key|authorization)", key, re.I)
           for key in parse_qs(parsed.query)):
        raise IntegrationError("Keep credentials out of URLs. Use the bearer token or OAuth fields.")
    return value


def _local(url: str) -> bool:
    return urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}


async def _validate_destination(url: str, *, allow_local: bool) -> None:
    _url(url)
    host = urlsplit(url).hostname
    if _local(url):
        if allow_local:
            return
        raise IntegrationError("A remote server cannot redirect authorization to a local address.")
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError:
        raise IntegrationError("The MCP server address could not be resolved.") from None
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise IntegrationError("Remote MCP connections require a public address; local servers use localhost.")


def _schema(schema: dict) -> None:
    stack, count = [schema], 0
    while stack:
        value = stack.pop()
        count += 1
        if count > 20000:
            raise IntegrationError("MCP tool schema exceeds the complexity limit.")
        if isinstance(value, dict):
            for key in ("$id", "$ref", "$dynamicRef", "$recursiveRef"):
                if key in value and (not isinstance(value[key], str) or not value[key].startswith("#")):
                    raise IntegrationError("MCP tool schemas may only reference local definitions.")
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.exceptions.SchemaError:
        raise IntegrationError("The MCP server advertised an invalid tool schema.") from None


def _read_only(tool: dict) -> bool:
    annotations = tool.get("annotations") or {}
    return annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True


def _clean_catalog(rows: list[dict]) -> list[dict]:
    if len(rows) > MAX_TOOLS:
        raise IntegrationError("The MCP server advertises more than 500 tools.")
    names, catalog = set(), []
    for row in rows:
        name, schema = row.get("name"), row.get("inputSchema")
        if not isinstance(name, str) or not TOOL_NAME.fullmatch(name) or name in names:
            raise IntegrationError("The MCP server advertised an invalid or duplicate tool name.")
        if not isinstance(schema, dict):
            raise IntegrationError("The MCP server advertised an invalid tool schema.")
        _schema(schema)
        names.add(name)
        catalog.append({"name": name, "description": str(row.get("description") or "")[:12000],
                        "inputSchema": schema, "annotations": row.get("annotations") or {}})
    return catalog


def _secrets(record: dict) -> list[str]:
    values = [record.get("bearer_token"), record.get("client_secret"),
              (record.get("tokens") or {}).get("access_token"),
              (record.get("tokens") or {}).get("refresh_token"),
              (record.get("client_info") or {}).get("client_secret")]
    return [value for value in values if isinstance(value, str) and value]


def _redact(value, secrets_: list[str]):
    if isinstance(value, str):
        for secret in sorted({item for item in secrets_ if item}, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {_redact(key, secrets_): _redact(item, secrets_) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets_) for item in value]
    return value


def _registration_error(exc: OAuthRegistrationError) -> IntegrationError:
    """Extract only bounded, recognized diagnostics from SDK-owned messages.

    SDK 2.3 stores the registration HTTP status and response body in the exception
    string rather than structured attributes. The prefix is SDK-generated; the
    body remains untrusted and contributes only an allowlisted OAuth error code.
    """
    message = str(exc)[:8192]
    match = re.match(r"^Registration failed: ([1-5][0-9]{2})(?:\s|$)", message)
    if match:
        status = int(match.group(1))
        code = None
        body = message[match.end():].strip()
        if body.startswith("{"):
            try:
                parsed = json.loads(body)
            except (ValueError, RecursionError):
                parsed = None
            if (isinstance(parsed, dict) and isinstance(parsed.get("error"), str)
                    and parsed["error"] in _REGISTRATION_ERROR_CODES):
                code = parsed["error"]
        detail = f"HTTP {status}" + (f", {code}" if code else "")
        if status == 429 or code == "temporarily_unavailable":
            text = f"The authorization server temporarily limited OAuth client registration ({detail}). Try again later."
        elif status in {404, 405, 501} or code == "registration_not_supported":
            text = (f"The OAuth client registration endpoint is unavailable ({detail}). "
                    "Check the server endpoint or configure a registered client ID.")
        elif status >= 500 or code == "server_error":
            text = (f"The authorization server failed during OAuth client registration ({detail}). "
                    "Try again later or check the server's OAuth setup.")
        elif status in {401, 403} or code in {"access_denied", "unauthorized_client", "unapproved_software_statement"}:
            text = (f"The authorization server denied OAuth client registration ({detail}). "
                    "Check its registration policy or configure a client ID registered for Workbench.")
        elif code in {"invalid_client_metadata", "invalid_redirect_uri", "invalid_software_statement"}:
            text = (f"The authorization server rejected the OAuth client metadata ({detail}). "
                    "Check the client configuration and registered callback URL.")
        elif 300 <= status < 400:
            text = (f"The authorization server redirected OAuth client registration unexpectedly ({detail}). "
                    "Check the server's OAuth endpoint configuration.")
        else:
            text = (f"The authorization server rejected OAuth client registration ({detail}). "
                    "Check the client configuration or configure a registered client ID.")
        return IntegrationError(text)
    if message.startswith("Invalid registration response:"):
        return IntegrationError("The authorization server returned an invalid OAuth client registration response. "
                                "Check the server's OAuth configuration.")
    if message.startswith("Authorization server registered the client with unsupported token_endpoint_auth_method "):
        return IntegrationError("The authorization server registered a client with an authentication method "
                                "Workbench cannot use. Check the server's OAuth client configuration.")
    if re.match(r"^Authorization server registered the client for '[a-z_]+' but issued no client_secret$", message):
        return IntegrationError("The authorization server registered a confidential client without its required secret. "
                                "Check the server's OAuth registration response.")
    return IntegrationError(_REGISTRATION_FAILURE)


def _authentication_failure(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_authentication_failure(child) for child in exc.exceptions)
    if isinstance(exc, OAuthFlowError):
        return True
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code in {401, 403}
    return isinstance(exc, IntegrationError) and "needs sign-in" in str(exc)


def _safe_error(exc: BaseException) -> IntegrationError:
    if isinstance(exc, IntegrationError):
        return IntegrationError(str(exc))
    if isinstance(exc, OAuthRegistrationError):
        return _registration_error(exc)
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            result = _safe_error(child)
            if str(result) != "The MCP request failed. Check the server connection and try again.":
                return result
    if isinstance(exc, (TimeoutError, Timeout)):
        return IntegrationError("The MCP request timed out. Check the server; tool calls are not replayed.")
    if isinstance(exc, httpx2.HTTPStatusError):
        code = exc.response.status_code
        if code in {401, 403}:
            return IntegrationError("The MCP server rejected access. Sign in again or check the account's permissions.")
        return IntegrationError(f"The MCP server returned HTTP {code}. Check its URL and supported transport.")
    # Inspect internally, but never expose SDK/provider exception bodies or URLs.
    message = str(exc).lower()
    if "state" in message and "mismatch" in message:
        return IntegrationError("The sign-in response did not match this connection. Start sign-in again.")
    if "issuer" in message:
        return IntegrationError("The authorization server identity did not match. Check the server's OAuth setup.")
    if "redirect" in message:
        return IntegrationError("The server rejected an OAuth redirect. Check the registered Workbench callback URL.")
    return IntegrationError("The MCP request failed. Check the server connection and try again.")


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > MAX_BYTES:
                raise IntegrationError("MCP response exceeded 16 MiB. Narrow the request.")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class _GuardedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, allow_local: bool):
        self.allow_local = allow_local
        self.inner = httpx2.AsyncHTTPTransport(trust_env=False, retries=0)
        # httpx2 exposes no network-backend argument. Replace only the dialer;
        # httpcore retains the original origin for TLS SNI and certificate checks.
        pool = self.inner._pool
        pool._network_backend = _PinnedBackend(pool._network_backend, allow_local)

    async def handle_async_request(self, request):
        await _validate_destination(str(request.url), allow_local=self.allow_local)
        response = await self.inner.handle_async_request(request)
        response.stream = _BoundedStream(response.stream)
        return response

    async def aclose(self):
        await self.inner.aclose()


class _PinnedBackend:
    """Resolve and validate the exact numeric address handed to the socket."""

    def __init__(self, inner, allow_local):
        self.inner, self.allow_local = inner, allow_local

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        local = host in {"localhost", "127.0.0.1", "::1"}
        try:
            addresses = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise IntegrationError("The MCP server address could not be resolved.") from None
        ips = list(dict.fromkeys(row[4][0] for row in addresses))
        allowed = [(self.allow_local and ipaddress.ip_address(ip).is_loopback)
                   if local else ipaddress.ip_address(ip).is_global for ip in ips]
        if not ips or not all(allowed):
            raise IntegrationError("MCP connections cannot reach private network addresses.")
        last_error = None
        for ip in ips:
            try:
                return await self.inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                    socket_options=socket_options)
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise IntegrationError("The MCP server address could not be reached.")

    async def connect_unix_socket(self, *args, **kwargs):
        raise IntegrationError("Remote MCP connections cannot open local sockets.")

    async def sleep(self, seconds):
        await self.inner.sleep(seconds)


@dataclass
class _ConnectJob:
    key: tuple[str, str]
    generation: str
    redirect_uri: str
    expires_at: float = field(default_factory=lambda: time.time() + AUTH_SECONDS)
    changed: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    callback_ready: threading.Event = field(default_factory=threading.Event)
    authorization_url: str | None = None
    expected_state: str | None = None
    callback: dict | None = None
    consumed: bool = False
    canceled: bool = False
    error: str | None = None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None


class _TokenStorage:
    def __init__(self, owner: RemoteMCP, id_: str, generation: str):
        self.owner, self.id, self.generation = owner, id_, generation
        self.provider = None
        self.seen_secrets: list[str] = []

    def record(self):
        record = self.owner._record(self.id)
        if record["generation"] != self.generation:
            raise IntegrationError("This MCP connection changed. Connect it again.")
        self.seen_secrets.extend(_secrets(record))
        return record

    async def get_tokens(self):
        value = self.record().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens):
        value = tokens.model_dump(mode="json", exclude_none=True)
        update = {"tokens": value,
                  "expires_at": time.time() + tokens.expires_in if tokens.expires_in is not None else None}
        if self.provider:
            context = self.provider.context
            update["oauth_metadata"] = (context.oauth_metadata.model_dump(mode="json", exclude_none=True)
                                        if context.oauth_metadata else None)
            update["protected_resource_metadata"] = (
                context.protected_resource_metadata.model_dump(mode="json", exclude_none=True)
                if context.protected_resource_metadata else None)
            update["auth_server_url"] = context.auth_server_url
        self.owner._update(self.id, update, generation=self.generation)
        self.seen_secrets.extend([tokens.access_token, tokens.refresh_token or ""])

    async def get_client_info(self):
        record = self.record()
        value = record.get("client_info")
        if value:
            return OAuthClientInformationFull.model_validate(value)
        if record.get("client_id"):
            return OAuthClientInformationFull(
                client_id=record["client_id"], client_secret=record.get("client_secret"),
                redirect_uris=[record["redirect_uri"]],
                token_endpoint_auth_method="client_secret_post" if record.get("client_secret") else "none",
                scope=record.get("scope") or None)
        return None

    async def set_client_info(self, client_info):
        self.owner._update(self.id, {"client_info": client_info.model_dump(mode="json", exclude_none=True)},
                           generation=self.generation)
        if client_info.client_secret:
            self.seen_secrets.append(client_info.client_secret)


class _PersistentOAuth(OAuthClientProvider):
    """The SDK does not restore absolute expiry/discovery across fresh sessions."""

    async def _initialize(self):
        await super()._initialize()
        record = self.context.storage.record()
        self.context.token_expiry_time = record.get("expires_at")
        if record.get("oauth_metadata"):
            self.context.oauth_metadata = OAuthMetadata.model_validate(record["oauth_metadata"])
        if record.get("protected_resource_metadata"):
            self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                record["protected_resource_metadata"])
        self.context.auth_server_url = record.get("auth_server_url")

    async def _perform_authorization_code_grant(self):
        # SDK discovery selects advertised scopes. An explicit human selection
        # remains authoritative instead of silently expanding to the full list.
        scope = self.context.storage.record().get("scope")
        if scope:
            self.context.client_metadata.scope = scope
        return await super()._perform_authorization_code_grant()


class RemoteMCP:
    def __init__(self, runs_dir: Path):
        self.store = Store(Path(runs_dir), namespace="mcp")
        self.key = str(self.store.root.resolve())

    def _record(self, id_: str) -> dict:
        if not isinstance(id_, str) or not CONNECTION_ID.fullmatch(id_):
            raise IntegrationError("Unknown MCP connection.")
        with self.store.lock:
            record = self.store.read().get("connections", {}).get(id_)
        if not isinstance(record, dict):
            raise IntegrationError("Unknown MCP connection.")
        return copy.deepcopy(record)

    def _update(self, id_: str, update: dict, *, generation: str | None = None) -> dict:
        with self.store.lock:
            value = self.store.read()
            record = value.get("connections", {}).get(id_)
            if record is None or (generation is not None and record["generation"] != generation):
                raise IntegrationError("This MCP connection changed. Connect it again.")
            record.update(update)
            self.store.write(value)
            return copy.deepcopy(record)

    def _public(self, record: dict) -> dict:
        state = record.get("state", "disconnected")
        message = record.get("message", "Connect this server to discover its tools.")
        if message == _LEGACY_REGISTRATION_MESSAGE:
            message = _REGISTRATION_FAILURE
        if state == "connecting":
            with _jobs_lock:
                job = _jobs.get((self.key, record["id"]))
            if job is None or job.expires_at < time.time():
                state = "disconnected"
                message = "Sign-in was interrupted or expired. Connect this server again."
        enabled = set(record.get("enabled_tools", []))
        tools = [{**{key: row[key] for key in ("name", "description", "annotations")},
                  "enabled": row["name"] in enabled,
                  "read_only": _read_only(row)}
                 for row in record.get("catalog", [])]
        return {"id": record["id"], "name": record["name"], "kind": "mcp", "generic": True,
                "state": state, "available": True, "connected": state == "connected",
                "tools_count": len([row for row in tools if row["enabled"]]), "catalog_count": len(tools),
                "message": message,
                "url": record["url"], "auth": record["auth"], "transport": "streamable_http",
                "client_id": record.get("client_id", ""), "scope": record.get("scope", ""),
                "client_metadata_url": record.get("client_metadata_url", ""),
                "updated_at": record.get("updated_at"), "expires_at": record.get("expires_at"),
                "persistent": True, "tools": tools}

    def status(self) -> list[dict]:
        with self.store.lock:
            rows = list(self.store.read().get("connections", {}).values())
        return [self._public(row) for row in rows]

    def _config(self, body: dict, prior: dict | None = None) -> dict:
        if not isinstance(body, dict):
            raise IntegrationError("MCP configuration must be an object.")
        prior = prior or {}
        result = {}
        for key in ("name", "url", "auth", "client_id", "scope", "client_metadata_url"):
            default = "auto" if key == "auth" else ""
            result[key] = body.get(key, prior.get(key, default))
            if not isinstance(result[key], str):
                raise IntegrationError("MCP configuration fields must be text.")
            result[key] = result[key].strip()
        if not result["name"] or len(result["name"]) > 80:
            raise IntegrationError("Give the MCP connection a name of 1 to 80 characters.")
        result["url"] = _url(result["url"])
        if result["auth"] not in {"auto", "none", "oauth", "bearer"}:
            raise IntegrationError("Choose automatic, public, OAuth or bearer-token authentication.")
        if body.get("transport", "streamable_http") != "streamable_http":
            raise IntegrationError("Use a Streamable HTTP MCP endpoint. Local stdio commands are not supported.")
        if len(result["client_id"]) > 512 or len(result["scope"]) > 2000:
            raise IntegrationError("The OAuth client ID or scope is too long.")
        if result["client_metadata_url"]:
            _url(result["client_metadata_url"])
            if urlsplit(result["client_metadata_url"]).scheme != "https":
                raise IntegrationError("An OAuth client metadata document must use HTTPS.")
        for key in ("client_secret", "bearer_token"):
            value = body.get(key) or prior.get(key) or ""
            if not isinstance(value, str) or len(value) > 16384 or "\n" in value or "\r" in value:
                raise IntegrationError("The credential field is invalid.")
            result[key] = value
        if prior and result["url"] != prior["url"]:
            # A pasted replacement endpoint must never inherit another server's
            # bearer token or confidential OAuth client secret.
            for key in ("client_secret", "bearer_token"):
                result[key] = body.get(key) or ""
            if (result["auth"] in {"auto", "oauth"} and result["client_id"]
                    and prior.get("client_secret") and not result["client_secret"]):
                raise IntegrationError("Enter this server's OAuth client secret, or clear its client ID before changing the URL.")
        if prior and result["auth"] != prior["auth"]:
            if result["auth"] != "bearer":
                result["bearer_token"] = ""
            if result["auth"] not in {"auto", "oauth"}:
                result["client_secret"] = ""
        if result["auth"] == "bearer" and not result["bearer_token"]:
            raise IntegrationError("Enter a bearer token to connect this server.")
        if "enabled_tools" in body:
            enabled = body["enabled_tools"]
            if (not isinstance(enabled, list) or len(enabled) > MAX_TOOLS
                    or any(not isinstance(name, str) or not TOOL_NAME.fullmatch(name) for name in enabled)):
                raise IntegrationError("Choose tools from this server's catalog.")
            known = {row["name"] for row in prior.get("catalog", [])}
            if set(enabled) - known:
                raise IntegrationError("Refresh the server before enabling tools that are not in its catalog.")
            result["enabled_tools"] = list(dict.fromkeys(enabled))
            result["permissions_customized"] = True
            result["approved_nonread_tools"] = [row["name"] for row in prior.get("catalog", [])
                                                if row["name"] in enabled and not _read_only(row)]
        return result

    def add(self, body: dict) -> dict:
        config = self._config(body)
        id_ = "mcp_" + secrets.token_hex(6)
        record = {**config, "id": id_, "generation": secrets.token_hex(16),
                  "state": "disconnected", "catalog": [], "enabled_tools": []}
        with self.store.lock:
            value = self.store.read()
            rows = value.setdefault("connections", {})
            if len(rows) >= 32:
                raise IntegrationError("Workbench supports up to 32 saved MCP connections.")
            rows[id_] = record
            self.store.write(value)
        return self._public(record)

    def configure(self, id_: str, body: dict) -> dict:
        prior = self._record(id_)
        config = self._config(body, prior)
        identity = ("url", "auth", "client_id", "client_secret", "scope", "client_metadata_url", "bearer_token")
        if any(config.get(key) != prior.get(key, "") for key in identity):
            self.disconnect(id_)
            config.update({"catalog": [], "enabled_tools": [], "client_info": None,
                           "oauth_metadata": None, "protected_resource_metadata": None,
                           "auth_server_url": None, "permissions_customized": False,
                           "approved_nonread_tools": []})
        return self._public(self._update(id_, config))

    def disconnect(self, id_: str) -> dict:
        self._record(id_)
        with _jobs_lock:
            job = _jobs.pop((self.key, id_), None)
            if job:
                job.canceled = True
                job.callback_ready.set()
                if job.loop and job.task and job.loop.is_running() and not job.finished.is_set():
                    job.loop.call_soon_threadsafe(job.task.cancel)
        return self._public(self._update(id_, {"generation": secrets.token_hex(16), "state": "disconnected",
                                             "tokens": None, "expires_at": None, "message": "Disconnected."}))

    def remove(self, id_: str) -> dict:
        self.disconnect(id_)
        with self.store.lock:
            value = self.store.read()
            value["connections"].pop(id_, None)
            self.store.write(value)
        return {"id": id_, "removed": True}

    def connect(self, id_: str, redirect_uri: str) -> dict:
        _url(redirect_uri, callback=True)
        record = self._record(id_)
        with _jobs_lock:
            previous = _jobs.get((self.key, id_))
            if previous and not previous.finished.is_set() and not previous.canceled:
                return self._connection_result(previous)
            if record.get("redirect_uri") and record["redirect_uri"] != redirect_uri:
                record = self._update(id_, {"tokens": None, "expires_at": None, "client_info": None})
            generation = secrets.token_hex(16)
            self._update(id_, {"generation": generation, "redirect_uri": redirect_uri,
                              "state": "connecting", "message": "Connecting to the MCP server."})
            job = _ConnectJob((self.key, id_), generation, redirect_uri)
            _jobs[job.key] = job
            threading.Thread(target=self._connect_worker, args=(job,), daemon=True,
                             name="mcp-connect-" + id_).start()
        job.changed.wait(12)
        return self._connection_result(job)

    def _connection_result(self, job: _ConnectJob) -> dict:
        if job.error:
            raise IntegrationError(job.error)
        result = self._public(self._record(job.key[1]))
        if job.authorization_url and not job.finished.is_set() and not job.consumed:
            result["authorization_url"] = job.authorization_url
        return result

    def _connect_worker(self, job):
        try:
            self._run(job.key[1], "catalog", job=job)
        except BaseException as exc:
            error = ("Sign-in was canceled. Connect again when ready." if job.canceled
                     else str(_safe_error(exc)))
            job.error = error
            try:
                self._update(job.key[1], {"state": "error", "message": error}, generation=job.generation)
            except IntegrationError:
                pass
        finally:
            job.finished.set()
            job.changed.set()

    def complete_auth(self, query: dict, redirect_uri: str) -> dict:
        _url(redirect_uri, callback=True)
        state = query.get("state")
        if not isinstance(state, str) or not state or len(state) > 512:
            raise IntegrationError("The sign-in response has no valid state. Start sign-in again.")
        with _jobs_lock:
            job = next((job for key, job in _jobs.items() if key[0] == self.key and job.expected_state
                        and secrets.compare_digest(state, job.expected_state)), None)
            if (not job or job.canceled or job.consumed or job.finished.is_set()
                    or job.expires_at <= time.time() or job.redirect_uri != redirect_uri):
                raise IntegrationError("This sign-in link expired or was already used. Connect again.")
            code, issuer = query.get("code", ""), query.get("iss")
            if not isinstance(code, str) or len(code) > 8192 or (issuer is not None and not isinstance(issuer, str)):
                raise IntegrationError("The authorization server returned an invalid callback.")
            job.consumed = True
            job.callback = {"code": code, "state": state, "iss": issuer, "error": bool(query.get("error"))}
            job.callback_ready.set()
        job.finished.wait(25)
        return self._connection_result(job)

    def refresh(self, id_: str) -> dict:
        self._run(id_, "catalog")
        return self._public(self._record(id_))

    def tools(self, id_: str, query="", limit=30, offset=0) -> dict:
        if (not isinstance(query, str) or len(query) > 1000
                or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50
                or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0):
            raise IntegrationError("Use a text query, a limit of 1 to 50, and a nonnegative offset.")
        record = self._record(id_)
        if record.get("state") != "connected":
            raise IntegrationError("Connect this MCP server in Integrations before using its tools.")
        if time.time() - record.get("updated_at", 0) > 300:
            self.refresh(id_)
            record = self._record(id_)
        enabled = set(record.get("enabled_tools", []))
        rows = [row for row in record.get("catalog", []) if row["name"] in enabled
                and query.lower() in (row["name"] + " " + row["description"]).lower()]
        result = [{**row, "name": id_ + ":" + row["name"],
                   "dispatch_tool": "integration_call" if _read_only(row) else "integration_execute"}
                  for row in rows[offset:offset + limit]]
        return {"integration": id_, "tools": result, "total": len(rows),
                "next_offset": offset + limit if offset + limit < len(rows) else None}

    def call(self, tool: str, arguments: dict, *, read_only=False) -> dict:
        if not isinstance(tool, str):
            raise IntegrationError("Choose an MCP tool from integration_tools.")
        id_, separator, remote_name = tool.partition(":")
        if not separator or not TOOL_NAME.fullmatch(remote_name):
            raise IntegrationError("Choose an MCP tool from integration_tools.")
        record = self._record(id_)
        if record.get("state") != "connected":
            raise IntegrationError("Connect this MCP server in Integrations before calling its tools.")
        if not isinstance(arguments, dict):
            raise IntegrationError("MCP tool arguments must be an object.")
        try:
            size = len(json.dumps(arguments, allow_nan=False).encode())
        except (ValueError, TypeError):
            raise IntegrationError("MCP tool arguments must contain valid JSON values.") from None
        if size > 256 * 1024:
            raise IntegrationError("MCP tool arguments exceed the 256 KiB limit.")
        result = self._run(id_, "call", remote_name=remote_name, arguments=arguments, read_only=read_only)
        return {"result": result, "gateway": record["url"], "provider": id_}

    def _run(self, id_: str, operation: str, *, job=None, **kwargs):
        record = self._record(id_)
        generation = job.generation if job else record["generation"]
        lock_path = self.store.root / (id_ + ".operation.lock")
        if lock_path.is_symlink():
            raise IntegrationError("Integration lock must not be a symlink.")
        try:
            with FileLock(lock_path, timeout=2):
                return asyncio.run(self._operation(id_, generation, operation, job=job, **kwargs))
        except Exception as exc:
            error = _safe_error(exc)
            if _authentication_failure(exc):
                try:
                    self._update(id_, {"state": "error", "message": str(error)}, generation=generation)
                except IntegrationError:
                    pass
            raise error from None

    def _publish_catalog(self, id_: str, generation: str, catalog: list[dict], secrets_: list[str]) -> set[str]:
        """Publish discovery against the latest human permissions under one lock."""
        with self.store.lock:
            value = self.store.read()
            record = value.get("connections", {}).get(id_)
            if record is None or record["generation"] != generation:
                raise IntegrationError("This MCP connection changed. Connect it again.")
            old_names = {row["name"] for row in record.get("catalog", [])}
            permitted = set(record.get("approved_nonread_tools", []))
            enabled = set(record.get("enabled_tools", []))
            enabled.intersection_update(row["name"] for row in catalog
                                        if _read_only(row) or row["name"] in permitted)
            if not record.get("permissions_customized"):
                enabled.update(row["name"] for row in catalog
                               if row["name"] not in old_names and _read_only(row))
            record.update({"catalog": _redact(catalog, secrets_), "enabled_tools": sorted(enabled),
                           "state": "connected", "updated_at": time.time(),
                           "message": "Connected directly to this MCP server."})
            self.store.write(value)
            return enabled

    async def _operation(self, id_, generation, operation, *, job=None, remote_name=None,
                         arguments=None, read_only=False):
        _private_flow.set(True)
        storage = _TokenStorage(self, id_, generation)
        record = storage.record()
        if job:
            job.loop, job.task = asyncio.get_running_loop(), asyncio.current_task()
        auth, headers = None, {}

        async def redirect(url):
            if job is None:
                raise IntegrationError("This server needs sign-in. Connect it again in Integrations.")
            await _validate_destination(url, allow_local=_local(record["url"]))
            state = parse_qs(urlsplit(url).query).get("state", [None])[0]
            if not state:
                raise IntegrationError("The OAuth authorization URL has no state.")
            with _jobs_lock:
                if job.canceled:
                    raise IntegrationError("Sign-in was canceled.")
                job.expected_state, job.authorization_url = state, url
                self._update(id_, {"message": "Finish sign-in in the browser."}, generation=generation)
                job.changed.set()

        async def callback():
            while not job.callback_ready.is_set():
                if time.time() > job.expires_at:
                    raise IntegrationError("Sign-in expired. Connect this MCP server again.")
                await asyncio.sleep(0.1)
            if job.canceled or not job.callback or job.callback.get("error"):
                raise IntegrationError("Sign-in was canceled or denied. Connect again when ready.")
            return AuthorizationCodeResult(**{key: job.callback[key] for key in ("code", "state", "iss")})

        if record["auth"] == "bearer":
            headers["Authorization"] = "Bearer " + record["bearer_token"]
        elif record["auth"] in {"auto", "oauth"}:
            metadata = OAuthClientMetadata(client_name="Video Workbench",
                                           redirect_uris=[record["redirect_uri"]],
                                           token_endpoint_auth_method=(
                                               "client_secret_post" if record.get("client_secret") else "none"),
                                           scope=record.get("scope") or None)
            auth = _PersistentOAuth(record["url"], metadata, storage, redirect, callback,
                                    client_metadata_url=record.get("client_metadata_url") or None)
            storage.provider = auth

        async with asyncio.timeout(AUTH_SECONDS if job else 150):
            async with httpx2.AsyncClient(auth=auth, headers=headers, trust_env=False, follow_redirects=False,
                                         timeout=httpx2.Timeout(60, connect=10),
                                         transport=_GuardedTransport(_local(record["url"]))) as client:
                async with streamable_http_client(record["url"], http_client=client) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=AUTH_SECONDS if job else 120,
                                             client_info=types.Implementation(name="video-workbench", version="1")) as session:
                        await session.initialize()
                        rows, cursor, seen = [], None, set()
                        for _ in range(20):
                            page = await session.list_tools(
                                params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
                            rows.extend(item.model_dump(mode="json", by_alias=True, exclude_none=True,
                                                        exclude_unset=True) for item in page.tools)
                            if len(rows) > MAX_TOOLS:
                                raise IntegrationError("The MCP server advertises more than 500 tools.")
                            cursor = page.next_cursor
                            if not cursor:
                                break
                            if cursor in seen or len(cursor) > 2000:
                                raise IntegrationError("The MCP server returned an invalid catalog cursor.")
                            seen.add(cursor)
                        else:
                            raise IntegrationError("The MCP tool catalog exceeded 20 pages.")
                        catalog = _clean_catalog(rows)
                        enabled = self._publish_catalog(id_, generation, catalog, storage.seen_secrets)
                        if operation == "catalog":
                            return None
                        row = next((row for row in catalog if row["name"] == remote_name), None)
                        if not row or remote_name not in enabled:
                            raise IntegrationError("Enable this tool in Integrations before calling it.")
                        if read_only and not _read_only(row):
                            raise IntegrationError("This tool may change data. Use integration_execute after enabling it.")
                        try:
                            jsonschema.validate(arguments, row["inputSchema"])
                        except (jsonschema.ValidationError, jsonschema.SchemaError):
                            raise IntegrationError("Arguments do not match the MCP tool's input schema.") from None
                        # Recheck immediately before execution after any disconnect/permission change.
                        latest = storage.record()
                        if remote_name not in latest.get("enabled_tools", []):
                            raise IntegrationError("This tool was disabled before the call could run.")
                        result = await session.call_tool(remote_name, arguments)
                        storage.record()
                        value = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if len(json.dumps(value).encode()) > MAX_BYTES:
                            raise IntegrationError("MCP result exceeded 16 MiB. Narrow the request.")
                        return _redact(value, storage.seen_secrets)
