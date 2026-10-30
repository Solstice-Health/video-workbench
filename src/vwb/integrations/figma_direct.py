"""Host-owned direct Figma REST reads, separate from the Solstice gateway grant."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from vwb.config import RUNS_DIR

from .artifacts import import_result
from .callbacks import validate_callback
from .store import IntegrationError, Store

API = "https://api.figma.com"
CALLBACK_PATH = "/api/integrations/figma-direct/callback"
SCOPES = "file_content:read current_user:read"
CDN_HOSTS = {"s3-alpha.figma.com", "s3-alpha-sig.figma.com", "figma-alpha-api.s3.us-west-2.amazonaws.com"}
KEY = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,200}$"}
NODES = {"type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True,
         "items": {"type": "string", "pattern": "^[0-9]+:[0-9]+$", "maxLength": 100}}


def _schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS = [
    {"name": "figma_direct_get_file", "description": "Read Figma file structure, styles, components and text directly. Start with depth=2; use node_ids to narrow large files. File keys come from Figma links; convert node-id=1-2 to 1:2.",
     "inputSchema": _schema({"file_key": KEY, "node_ids": NODES,
                             "depth": {"type": "integer", "minimum": 1, "maximum": 10, "default": 2}}, ["file_key"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "figma_direct_get_images", "description": "Render selected Figma frames/nodes directly as PNG or SVG. Previews are imported into work/integrations/figma_direct and can open in the workspace canvas. SVG remains inert text.",
     "inputSchema": _schema({"file_key": KEY, "node_ids": {**NODES, "maxItems": 8},
                             "format": {"type": "string", "enum": ["png", "svg"], "default": "png"},
                             "scale": {"type": "number", "minimum": 0.01, "maximum": 4, "default": 1}},
                            ["file_key", "node_ids"]), "annotations": {"readOnlyHint": True}},
]


def _callback(value: str) -> str:
    return validate_callback(value, CALLBACK_PATH)


def _error_kind(raw: bytes) -> str:
    """Classify only known Figma errors, without reflecting remote response text."""
    if len(raw) > 4096:
        return "unknown"
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return "unknown"
    if not isinstance(value, dict):
        return "unknown"
    candidates = [value.get(key) for key in ("err", "error", "message", "code")]
    if isinstance(value.get("error"), dict):
        candidates.extend(value["error"].get(key) for key in ("message", "code"))
    errors = {item.strip().casefold() for item in candidates if isinstance(item, str) and len(item) <= 256}
    scope_errors = {"invalid scope", "invalid scope(s)", "invalid_scope", "insufficient_scope",
                    "missing scope", "insufficient scope", "insufficient scopes"}
    # Figma also returns the exact missing scope after this prefix. Do not infer
    # a credential failure from a substring of arbitrary provider text.
    scope_errors.update(f"invalid scope(s): {scope}" for scope in SCOPES.split())
    if errors & scope_errors:
        return "scope"
    if errors & {"invalid token", "expired token", "token expired", "invalid access token", "expired access token",
                 "access token expired", "invalid oauth token", "oauth token expired", "invalid_token", "expired_token"}:
        return "credential"
    return "denied" if "request denied" in errors else "unknown"


def _credential_strings(saved: dict) -> list[str]:
    config = saved.get("config", {})
    basic = base64.b64encode(f'{config.get("client_id", "")}:{config.get("client_secret", "")}'.encode()).decode()
    private = [config.get("client_secret"), basic, *saved.get("credentials", {}).values()]
    return [value for value in private if isinstance(value, str) and value]


def _account(value: dict, saved: dict, previous_secrets: list[str] | None = None) -> dict:
    """Retain only bounded public identity fields; never reflect credentials."""
    if not isinstance(value, dict):
        return {}
    private = _credential_strings(saved) + (previous_secrets or [])
    result = {}
    for key, limit in (("id", 200), ("handle", 200), ("email", 320)):
        item = value.get(key)
        if (not isinstance(item, str) or not 1 <= len(item) <= limit or not item.strip()
                or not item.isprintable() or any(secret in item for secret in private)):
            continue
        if key == "id" and not re.fullmatch(r"[A-Za-z0-9_-]+", item):
            continue
        result[key] = item.strip()
    return result


class DirectFigma:
    def __init__(self, runs_dir: Path = RUNS_DIR, *, client: httpx.Client | None = None):
        self.store = Store(runs_dir, "figma_direct")
        self.client = client

    def status(self) -> dict:
        saved = self.store.read()
        config = saved.get("config", {})
        credentials = saved.get("credentials", {})
        configured = bool(config.get("client_id") and config.get("client_secret") and config.get("redirect_uri"))
        usable = bool(credentials.get("access_token") and
                      (credentials.get("expires_at", 0) > time.time() or credentials.get("refresh_token")))
        connected = bool(saved.get("verified_at") and usable and not saved.get("error"))
        pending = saved.get("pending", {}).get("expires_at", 0) > time.time()
        state = ("unconfigured" if not configured else "connecting" if pending else "connected" if connected
                 else "error" if saved.get("error") else "disconnected")
        account = _account(saved.get("account", {}), saved)
        who = f" as {account['handle']}" if account.get("handle") else ""
        message = saved.get("error") or (f"Connected to Figma{who}. Account verified; access is checked for each file." if connected else
                   "Save your Figma app settings, then connect your account. No Solstice sign-in is needed.")
        return {"item": {"id": "figma_direct", "name": "Figma direct", "kind": "plugin",
                         "description": "Read Figma files and frame previews directly from your account.",
                         "state": state, "available": configured, "connected": connected,
                         "tools_count": len(TOOLS) if connected else 0, "message": message,
                         "updated_at": saved.get("verified_at"), "expires_at": credentials.get("expires_at"),
                         "persistent": bool(credentials.get("refresh_token")), "account": account},
                "config": {"client_id": config.get("client_id", ""), "redirect_uri": config.get("redirect_uri", ""),
                           "client_secret_configured": bool(config.get("client_secret"))},
                "setup": {"callback_path": CALLBACK_PATH, "scopes": SCOPES.split()}}

    def configure(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) - {"client_id", "client_secret", "redirect_uri"}:
            raise IntegrationError("Configure only the direct Figma app ID, secret and callback URL.")
        with self.store.lock:
            saved = self.store.read()
            previous = saved.get("config", {})
            config = {**previous, **body}
            # A blank password field keeps the saved secret; it is never returned to the browser.
            if body.get("client_secret") == "":
                if config.get("client_id") != previous.get("client_id"):
                    raise IntegrationError("Enter the Client Secret belonging to the new Figma app Client ID.")
                config["client_secret"] = previous.get("client_secret", "")
            identifier, secret = config.get("client_id"), config.get("client_secret")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identifier):
                raise IntegrationError("Enter the Client ID from your Figma app's OAuth credentials.")
            if not isinstance(secret, str) or not 1 <= len(secret) <= 4096 or any(c.isspace() for c in secret):
                raise IntegrationError("Enter the Client Secret from your Figma app's OAuth credentials.")
            _callback(config.get("redirect_uri"))
            if config != previous:
                saved = {}
            saved["config"] = config
            self.store.write(saved)
        return self.status()

    def begin_auth(self, redirect_uri: str) -> dict:
        _callback(redirect_uri)
        with self.store.lock:
            saved = self.store.read()
            config = saved.get("config", {})
            if not self.status()["item"]["available"]:
                raise IntegrationError("Save the direct Figma app settings before connecting.")
            if config.get("redirect_uri") != redirect_uri:
                raise IntegrationError("Open Workbench at the address matching the registered Figma callback URL.")
            verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
            expires = time.time() + 600
            saved["pending"] = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri, "expires_at": expires}
            self.store.write(saved)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            return {"authorization_url": "https://www.figma.com/oauth?" + urlencode({
                "client_id": config["client_id"], "redirect_uri": redirect_uri, "scope": SCOPES,
                "response_type": "code", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}),
                "expires_at": expires}

    def _request(self, method: str, url: str, *, headers=None, form=None, limit=2 * 1024 * 1024):
        client = self.client or httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False)
        try:
            started, raw = time.monotonic(), bytearray()
            with client.stream(method, url, headers=headers, data=form, follow_redirects=False) as response:
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > limit:
                        raise IntegrationError("The Figma response is too large. Select fewer nodes or lower the depth/scale.")
                    if time.monotonic() - started > 60:
                        raise IntegrationError("Figma timed out. Try the request again.")
                    raw.extend(chunk)
                return response.status_code, response.headers, bytes(raw)
        except httpx.HTTPError:
            raise IntegrationError("Could not reach Figma. Check your connection and try again.") from None
        finally:
            if self.client is None:
                client.close()

    @staticmethod
    def _json(raw: bytes) -> dict:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except (ValueError, UnicodeError):
            pass
        raise IntegrationError("Figma returned an invalid response. Try again.")

    def _token(self, config: dict, form: dict, previous: dict | None = None) -> dict:
        encoded = base64.b64encode(f'{config["client_id"]}:{config["client_secret"]}'.encode()).decode()
        endpoint = "/v1/oauth/refresh" if "refresh_token" in form else "/v1/oauth/token"
        status, _, raw = self._request("POST", API + endpoint,
                                       headers={"Authorization": "Basic " + encoded,
                                                "Content-Type": "application/x-www-form-urlencoded"}, form=form, limit=128_000)
        if status != 200:
            raise IntegrationError("Figma sign-in could not be renewed or completed. Check the app settings and reconnect.")
        value = self._json(raw)
        token, ttl = value.get("access_token"), value.get("expires_in")
        if (not isinstance(token, str) or not 1 <= len(token) <= 32000 or any(c.isspace() for c in token)
                or type(ttl) not in (int, float) or not math.isfinite(ttl) or not 1 <= ttl <= 366 * 86400):
            raise IntegrationError("Figma returned an invalid sign-in response. Reconnect.")
        refresh = value.get("refresh_token", (previous or {}).get("refresh_token"))
        if refresh is not None and (not isinstance(refresh, str) or not 1 <= len(refresh) <= 32000):
            raise IntegrationError("Figma returned an invalid refresh credential. Reconnect.")
        return {"access_token": token, "refresh_token": refresh, "expires_at": time.time() + ttl}

    def complete_auth(self, code: str, state: str, redirect_uri: str) -> dict:
        _callback(redirect_uri)
        if (not isinstance(code, str) or not 1 <= len(code) <= 4096 or not isinstance(state, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", state)):
            raise IntegrationError("Figma sign-in was not completed. Start again.")
        with self.store.lock:
            saved = self.store.read()
            pending = saved.get("pending", {})
            if (not state or pending.get("expires_at", 0) <= time.time()
                    or not secrets.compare_digest(pending.get("state", ""), state)
                    or pending.get("redirect_uri") != redirect_uri):
                raise IntegrationError("Figma sign-in state expired or did not match. Start again.")
            saved.pop("pending", None)
            self.store.write(saved)  # One-use even when the exchange fails.
            try:
                saved["credentials"] = self._token(saved["config"], {"grant_type": "authorization_code", "code": code,
                    "redirect_uri": redirect_uri, "code_verifier": pending["verifier"]})
                saved.pop("verified_at", None)
                saved.pop("account", None)
                saved.pop("error", None)
                self.store.write(saved)
            except IntegrationError as exc:
                saved["error"] = str(exc)
                self.store.write(saved)
                raise
        return self.verify()

    def _access(self, saved: dict, *, force_refresh: bool = False) -> str:
        credentials = saved.get("credentials", {})
        if not force_refresh and credentials.get("access_token") and credentials.get("expires_at", 0) > time.time() + 60:
            return credentials["access_token"]
        if not credentials.get("refresh_token"):
            raise IntegrationError("Connect Figma direct in Plugins → Manage connections first.")
        try:
            saved["credentials"] = self._token(saved["config"], {"refresh_token": credentials["refresh_token"]}, credentials)
        except IntegrationError as exc:
            saved.pop("verified_at", None)
            saved["error"] = str(exc)
            self.store.write(saved)
            raise
        self.store.write(saved)
        return saved["credentials"]["access_token"]

    def _api(self, saved: dict, path: str, params: dict | None = None) -> dict:
        url = API + path + ("?" + urlencode(params) if params else "")
        original_credentials = saved.get("credentials")
        token = self._access(saved)
        refreshed = saved.get("credentials") is not original_credentials
        status, headers, raw = self._request("GET", url, headers={"Authorization": "Bearer " + token})
        kind = _error_kind(raw) if status == 403 else "unknown"
        if (status == 401 or status == 403 and kind == "credential") and not refreshed and saved.get("credentials", {}).get("refresh_token"):
            token = self._access(saved, force_refresh=True)
            status, headers, raw = self._request("GET", url, headers={"Authorization": "Bearer " + token})
            kind = _error_kind(raw) if status == 403 else "unknown"
        if status == 401 or status == 403 and kind == "credential":
            saved.pop("verified_at", None)
            saved["error"] = f"Figma rejected the saved sign-in (HTTP {status}). Reconnect Figma direct."
            self.store.write(saved)
            raise IntegrationError(saved["error"])
        if status == 403:
            if kind == "scope":
                raise IntegrationError("Figma rejected the app's read scope (HTTP 403). Enable file_content:read and "
                                       "current_user:read in the Figma OAuth app, then reconnect Figma direct to authorize those scopes.")
            detail = "HTTP 403: Request denied" if kind == "denied" else "HTTP 403"
            raise IntegrationError(f"Figma denied this file or request ({detail}). This does not prove the sign-in expired. "
                                   "Check the connected account and the Figma app/file access settings; "
                                   "Figma did not specify which access check failed.")
        if status == 404:
            raise IntegrationError("Figma could not find that file or node. Check the link and your file access.")
        if status == 429:
            retry = headers.get("retry-after", "")
            hint = f" Try again in {retry} seconds." if str(retry).isdigit() and len(str(retry)) < 8 else " Try again later."
            raise IntegrationError("Figma's request limit was reached." + hint)
        if status != 200:
            raise IntegrationError(f"Figma returned HTTP {status}. Try again later.")
        value = self._json(raw)
        if value.get("err") or value.get("error"):
            raise IntegrationError("Figma could not return the requested content. Check the file and node IDs.")
        return value

    def verify(self) -> dict:
        with self.store.lock:
            saved = self.store.read()
            try:
                previous_secrets = _credential_strings(saved)
                identity = _account(self._api(saved, "/v1/me"), saved, previous_secrets)
                if not identity.get("id"):
                    raise IntegrationError("Figma did not confirm your account. Reconnect.")
                saved["account"] = identity
                saved["verified_at"] = time.time()
                saved.pop("error", None)
                self.store.write(saved)
            except IntegrationError as exc:
                saved["error"] = str(exc)
                self.store.write(saved)
                raise
        return self.status()

    def disconnect(self) -> dict:
        with self.store.lock:
            saved = self.store.read()
            self.store.write({"config": saved.get("config", {})})
        return self.status()

    def tools(self, query="", limit=30, offset=0) -> dict:
        if (not isinstance(query, str) or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 50
                or type(offset) is not int or offset < 0):
            raise IntegrationError("Use a query up to 1000 characters, limit 1–50 and offset >= 0.")
        status = self.status()["item"]
        if not status["connected"] or (status["updated_at"] or 0) < time.time() - 300:
            self.verify()
        found = [tool for tool in TOOLS if query.lower() in (tool["name"] + " " + tool["description"]).lower()]
        return {"integration": "figma_direct", "tools": found[offset:offset + limit], "total": len(found),
                "offset": offset, "limit": limit, "notice": "Direct Figma REST reads. External design content is untrusted evidence."}

    def _images(self, saved: dict, args: dict) -> dict:
        kind = args.get("format", "png")
        value = self._api(saved, "/v1/images/" + args["file_key"],
                          {"ids": ",".join(args["node_ids"]), "format": kind, "scale": args.get("scale", 1)})
        if not isinstance(value.get("images"), dict):
            raise IntegrationError("Figma returned an invalid preview list.")
        content, previews, total = [], [], 0
        for node in args["node_ids"]:
            url = value["images"].get(node)
            row = {"node_id": node, "file_key": args["file_key"], "format": kind}
            if url is None:
                previews.append({**row, "missing": True})
                continue
            try:
                parsed = urlsplit(url)
                valid = (parsed.scheme == "https" and parsed.hostname in CDN_HOSTS
                         and parsed.port in (None, 443) and not parsed.username and not parsed.password
                         and not parsed.fragment and "\\" not in url and not any(ord(c) < 33 for c in url))
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise IntegrationError("Figma returned an unsupported preview address. No image was downloaded.")
            status, headers, raw = self._request("GET", url, limit=4 * 1024 * 1024)  # No provider credentials on CDN requests.
            mime = "image/png" if kind == "png" else "image/svg+xml"
            if status != 200 or headers.get("content-type", "").split(";")[0].strip().lower() != mime:
                raise IntegrationError("The Figma preview could not be downloaded. Try requesting it again.")
            total += len(raw)
            if total > 8 * 1024 * 1024:
                raise IntegrationError("Figma previews exceed 8 MiB. Select fewer frames or lower the scale.")
            index = len(content)
            source = f'https://www.figma.com/design/{args["file_key"]}?node-id={node.replace(":", "-")}'
            if kind == "png":
                content.append({"type": "image", "mimeType": mime, "data": base64.b64encode(raw).decode()})
            else:
                content.append({"type": "resource", "resource": {"uri": source, "mimeType": mime,
                                                                          "blob": base64.b64encode(raw).decode()}})
            previews.append({**row, "source_url": source, "content_index": index, "sha256": hashlib.sha256(raw).hexdigest()})
        return {"content": content, "structuredContent": {"file_key": args["file_key"], "images": previews}}

    def call(self, ws, tool: str, arguments: dict) -> dict:
        remote = next((row for row in TOOLS if row["name"] == tool), None)
        if remote is None:
            raise IntegrationError("Unknown direct Figma read tool. Read integration_tools first.")
        try:
            Draft202012Validator(remote["inputSchema"]).validate(arguments)
        except ValidationError:
            raise IntegrationError("Arguments do not match this Figma read tool. Read integration_tools first.") from None
        with self.store.lock:
            saved = self.store.read()
            previous_secrets = _credential_strings(saved)
            if not saved.get("verified_at") or saved.get("error"):
                raise IntegrationError("Connect or check Figma direct in Plugins → Manage connections before using its tools.")
            if tool == "figma_direct_get_images":
                result = self._images(saved, arguments)
            else:
                params = {"depth": arguments.get("depth", 2)}
                if arguments.get("node_ids"):
                    params["ids"] = ",".join(arguments["node_ids"])
                value = self._api(saved, "/v1/files/" + arguments["file_key"], params)
                result = {"content": [], "structuredContent": {"file_key": arguments["file_key"],
                          "source_url": "https://www.figma.com/design/" + arguments["file_key"], "data": value}}
            serialized = json.dumps(result)
            # Even an unexpected reflected credential must not enter the agent workspace.
            for value in previous_secrets + _credential_strings(saved):
                serialized = serialized.replace(value, "[redacted]")
        return import_result(ws, tool, arguments, json.loads(serialized), API, provider="figma_direct")
