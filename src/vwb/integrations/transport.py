"""Bounded Streamable HTTP MCP client for the configured Solstice gateway only.

One short MCP session per operation. No sibling server traversal, sampling, roots,
elicitation, redirects, automatic retries, or credential forwarding to other origins.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .store import IntegrationError

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CATALOG_TOOLS = 500
PROTOCOL = "2025-06-18"


class Unauthorized(IntegrationError):
    pass


class HTTP:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client

    def request(self, method: str, url: str, *, headers=None, payload=None, form=None) -> tuple[int, dict, Any]:
        client = self.client or httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                                           trust_env=False)
        try:
            started = time.monotonic()
            with client.stream(method, url, headers=headers, json=payload, data=form) as response:
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise IntegrationError("Gateway response exceeded the 16 MiB limit; narrow the request.")
                    if time.monotonic() - started > 60:
                        raise IntegrationError("Gateway response timed out. The call was not replayed.")
                    chunks.extend(chunk)
                    # Streamable HTTP may keep SSE alive after the matching result.
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        for message in _sse(bytes(chunks)):
                            if isinstance(message, dict) and message.get("id") == (payload or {}).get("id"):
                                if "result" in message or "error" in message:
                                    return response.status_code, dict(response.headers), message
                if not chunks:
                    body = None
                else:
                    try:
                        body = json.loads(chunks)
                    except (ValueError, UnicodeError):
                        if response.status_code >= 300:
                            body = None
                        else:
                            raise IntegrationError("Gateway returned an invalid JSON response.") from None
                return response.status_code, dict(response.headers), body
        except httpx.HTTPError:
            raise IntegrationError("Gateway request failed. Check the connection; the call was not replayed.") from None
        finally:
            if self.client is None:
                client.close()


def _sse(raw: bytes):
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        return
    for event in text.split("\n\n")[:-1]:
        # SSE recognizes only CR/LF line endings. str.splitlines() also splits
        # Unicode separators inside otherwise valid JSON strings from Figma.
        data = "\n".join(line[5:].lstrip(" ") for line in event.split("\n") if line.startswith("data:"))
        if data:
            try:
                yield json.loads(data)
            except ValueError:
                raise IntegrationError("Gateway returned an invalid MCP event.") from None


class MCP:
    def __init__(self, http: HTTP, *, service_name: str = "Solstice", client_name: str = "solstice-video-workbench"):
        self.http = http
        self.service_name, self.client_name = service_name, client_name

    def execute(self, url: str, token: str | None, method: str, params: dict | None = None) -> dict:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        sequence = 0

        def rpc(name: str, arguments: dict | None = None, notification=False):
            nonlocal sequence
            sequence += 1
            body = {"jsonrpc": "2.0", "method": name}
            if not notification:
                body["id"] = sequence
            if arguments is not None:
                body["params"] = arguments
            status, received, reply = self.http.request("POST", url, headers=headers, payload=body)
            if status == 401:
                raise Unauthorized(f"{self.service_name} authorization expired or was rejected. Reconnect in Integrations.")
            if status == 403:
                raise IntegrationError(f"{self.service_name} denied this request. Check your account access and required permissions.")
            if status < 200 or status >= 300:
                raise IntegrationError(f"{self.service_name} returned HTTP {status}. The call was not replayed.")
            if notification:
                return None
            if not isinstance(reply, dict) or reply.get("id") != sequence or reply.get("jsonrpc") != "2.0":
                raise IntegrationError(f"{self.service_name} returned an invalid MCP response.")
            if "error" in reply:
                raise IntegrationError(f"{self.service_name} could not complete the MCP request. Check the tool and connection.")
            result = reply.get("result")
            if not isinstance(result, dict):
                raise IntegrationError(f"{self.service_name} returned an invalid MCP result.")
            if name == "initialize":
                session = received.get("mcp-session-id")
                if session:
                    if len(session) > 512 or any(ord(c) < 33 for c in session):
                        raise IntegrationError("Gateway returned an invalid MCP session identifier.")
                    headers["Mcp-Session-Id"] = session
                version = result.get("protocolVersion")
                if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                    raise IntegrationError("Gateway negotiated an unsupported MCP protocol version.")
                headers["MCP-Protocol-Version"] = version
            return result

        try:
            rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                               "clientInfo": {"name": self.client_name, "version": "0.1.0"}})
            rpc("notifications/initialized", notification=True)
            if method != "tools/list":
                return rpc(method, params or {})
            found, cursors = [], set()
            query = {}
            for _ in range(20):
                result = rpc("tools/list", query)
                tools = result.get("tools")
                if not isinstance(tools, list):
                    raise IntegrationError("Gateway returned an invalid tool catalog.")
                found.extend(tools)
                if len(found) > MAX_CATALOG_TOOLS:
                    raise IntegrationError("Gateway catalog exceeds the supported tool limit.")
                cursor = result.get("nextCursor")
                if not cursor:
                    return {"tools": found}
                if not isinstance(cursor, str) or len(cursor) > 2000 or cursor in cursors:
                    raise IntegrationError("Gateway returned an invalid catalog cursor.")
                cursors.add(cursor)
                query = {"cursor": cursor}
            raise IntegrationError("Gateway catalog exceeded the page limit.")
        finally:
            if "Mcp-Session-Id" in headers:
                try:
                    self.http.request("DELETE", url, headers=headers)
                except IntegrationError:
                    pass
