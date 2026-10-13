"""Control-plane services for agents running in containers.

  ModelProxy   an OpenAI-compatible endpoint. Containers authenticate with a per-run token;
               the proxy swaps in the real API key (invariant I3: vendor keys never enter the
               agent plane), streams the reply back, and logs every call with hashes (I4).
  ToolServer   the toolkit over MCP (streamable HTTP) for one agent, bound to its role, so a
               containerised Codex agent calls the same tool gateway as every other host.

Both listen on 127.0.0.1; Docker Desktop routes host.docker.internal to it.
"""

from __future__ import annotations

import base64
import json
import secrets as pysecrets
import socket
import threading
import time
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from vwb.config import secrets
from vwb.util.hashing import sha256_bytes
from vwb.workspace.ledger import CallLog

UPSTREAM = "https://api.openai.com/v1"


GENERATIVE_TOOLS = {"image_generation", "web_search", "web_search_preview", "code_interpreter", "computer_use_preview"}


def _refuse(path: str, body: bytes) -> str | None:
    """Containers use the model to reason and act through the toolkit. Generating media or reaching the web
    through the model API would skip the registry, provenance flags and the policy dial, so it is refused."""
    if path.startswith(("images", "videos", "audio/speech")):
        return (f"/{path} is not available to agents: make images and clips with the toolkit (sunburst_create, "
                "omni_video), which registers and flags them")
    if path.startswith("responses") and body:
        try:
            tools = json.loads(body).get("tools") or []
        except (ValueError, AttributeError):
            return None
        hosted = sorted({t.get("type") for t in tools if isinstance(t, dict)} & GENERATIVE_TOOLS)
        if hosted:
            return f"hosted tools {hosted} are not available to agents: use the toolkit's tools instead"
    return None


def _usage_from_stream(tail: bytes) -> dict[str, Any]:
    """Token usage from the last events of a Responses stream (or a plain JSON reply)."""
    text = tail.decode("utf-8", errors="ignore")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if '"usage"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        usage = (ev.get("response") or {}).get("usage") or ev.get("usage")
        if isinstance(usage, dict):
            return usage
    return {}


def _served_tier_from_stream(tail: bytes) -> str | None:
    for line in reversed(tail.decode("utf-8", errors="ignore").splitlines()):
        line = line.strip().removeprefix("data:").strip()
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        response = event.get("response") or event
        if isinstance(response, dict) and isinstance(response.get("service_tier"), str):
            return response["service_tier"]
    return None


def _tier_body(path: str, body: bytes, service_tier: str | None) -> bytes:
    """The host's selected tier overrides an agent's Responses request, never other routes."""
    if service_tier is None or path.split("?", 1)[0] != "responses":
        return body
    try:
        parsed = json.loads(body)
    except ValueError:
        return body
    if not isinstance(parsed, dict):
        return body
    parsed["service_tier"] = service_tier
    return json.dumps(parsed, separators=(",", ":")).encode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Bearer(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if request.headers.get("authorization", "") != f"Bearer {self.token}":
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        return await call_next(request)


class _Server:
    def __init__(self, app, port: int):
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="on", timeout_keep_alive=120))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"svc-{port}")

    def start(self) -> None:
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"service on port {self.port} did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class ModelProxy:
    """OpenAI-compatible pass-through that holds the key and logs every call."""

    def __init__(self, call_log: CallLog, token: str | None = None, actor: str = "codex-container", *,
                 service_tier: str | None = None):
        if service_tier not in (None, "default", "fast", "ultrafast"):
            raise ValueError("Unsupported OpenAI service tier.")
        self.token = token or pysecrets.token_urlsafe(24)
        self.actor = actor
        self.log = call_log
        self.port = free_port()
        key = secrets().require("openai")
        client = httpx.AsyncClient(timeout=httpx.Timeout(3900.0, connect=30.0))   # outlasts Codex's stream idle timeout

        async def health(_: Request) -> Response:
            return JSONResponse({"ok": True})

        async def forward(request: Request) -> Response:
            path = request.path_params["path"]
            body = await request.body()
            refusal = _refuse(path, body)
            if refusal:                                  # generation goes through the toolkit, so it is registered
                self.log.record(actor=self.actor, provider="openai", model="(refused)", purpose=f"proxy:{path}",
                                input_sha256=sha256_bytes(body), output_sha256=None, usage={}, seconds=0.0, ok=False,
                                error=refusal)
                return JSONResponse({"error": {"message": refusal, "type": "invalid_request_error"}}, status_code=403)
            if request.method == "POST":
                body = _tier_body(path, body, service_tier)
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "authorization", "content-length", "accept-encoding")}
            headers["authorization"] = f"Bearer {key}"
            headers["accept-encoding"] = "identity"   # stream bytes through unchanged (SSE)
            t0 = time.time()
            req = client.build_request(request.method, f"{UPSTREAM}/{path}", content=body, headers=headers,
                                       params=dict(request.query_params))
            upstream = await client.send(req, stream=True)
            digest = sha256_bytes(body)
            try:
                model = json.loads(body or b"{}").get("model") or "(unknown)"
            except (ValueError, AttributeError):
                model = "(unknown)"

            async def stream():
                size, tail, usage = 0, b"", {}
                try:
                    async for chunk in upstream.aiter_raw():
                        size += len(chunk)
                        tail = (tail + chunk)[-262144:]            # the last events carry the usage
                        yield chunk
                finally:
                    await upstream.aclose()
                    usage = _usage_from_stream(tail)
                    self.log.record(actor=self.actor, provider="openai", model=model, purpose=f"proxy:{path}",
                                    input_sha256=digest, output_sha256=None,
                                    usage={**usage, "bytes_out": size, "bytes_in": len(body),
                                           "service_tier_requested": service_tier,
                                           "service_tier_served": _served_tier_from_stream(tail)},
                                    seconds=round(time.time() - t0, 2), ok=upstream.status_code < 400,
                                    error="" if upstream.status_code < 400 else f"HTTP {upstream.status_code}")

            out_headers = {k: v for k, v in upstream.headers.items()
                           if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection")}
            return StreamingResponse(stream(), status_code=upstream.status_code, headers=out_headers)

        app = Starlette(routes=[Route("/health", health), Route("/v1/{path:path}", forward, methods=["GET", "POST", "DELETE"])],
                        middleware=[Middleware(_Bearer, token=self.token)])
        self._server = _Server(app, self.port)
        self._server.start()

    def stop(self) -> None:
        self._server.stop()


class ToolServer:
    """The toolkit for one agent over MCP (streamable HTTP), behind a bearer token."""

    def __init__(self, ctx, registry, role: str, token: str):
        import anyio
        import mcp.types as types
        from mcp.server.lowlevel import Server
        from mcp.server.transport_security import TransportSecuritySettings

        self.port = free_port()
        tools = registry.for_role(role)

        async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
            return types.ListToolsResult(tools=[types.Tool(name=t.name, description=t.description, input_schema=t.schema)
                                                for t in tools])

        async def on_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
            res = await anyio.to_thread.run_sync(registry.call, ctx, params.name, dict(params.arguments or {}))
            content: list[Any] = [types.TextContent(type="text", text=res.text or "(no text)")]
            for img in res.images[:6]:
                mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
                content.append(types.ImageContent.model_validate({"type": "image", "data": base64.b64encode(img).decode(),
                                                                 "mimeType": mime}))
            return types.CallToolResult(content=content, is_error=res.is_error)

        server = Server("video-workbench", version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool,
                        instructions=f"Video Workbench toolkit, role {role}.")
        mcp_app = server.streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True, json_response=True, host="127.0.0.1",
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        mcp_app.add_middleware(_Bearer, token=token)
        self._server = _Server(mcp_app, self.port)
        self._server.start()

    def stop(self) -> None:
        self._server.stop()
