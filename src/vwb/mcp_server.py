"""The toolkit over MCP (stdio), for any external agent host.

    vwb mcp serve --request <id> --role critic --agent critic-ext

Claude Code, Codex or another MCP client then calls the same tools, through the same tool
gateway (role allowlist, policy dial, call log, loop guards) as the built-in agents.
"""

from __future__ import annotations

import base64

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from vwb import __version__
from vwb.gateway import Gateway
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace


async def serve(request_id: str, role: str, agent: str) -> None:
    ws = Workspace.open(request_id)
    registry = build_registry()
    ctx = ToolContext(ws=ws, agent=agent, role=role, gateway=Gateway(ws, actor=agent))
    tools = registry.for_role(role)

    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool(name=t.name, description=t.description, input_schema=t.schema)
                                            for t in tools])

    async def on_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        res = await anyio.to_thread.run_sync(registry.call, ctx, params.name, dict(params.arguments or {}))
        content: list = [types.TextContent(type="text", text=res.text or "(no text)")]
        for img in res.images[:8]:
            mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
            content.append(types.ImageContent.model_validate({"type": "image", "data": base64.b64encode(img).decode(),
                                                             "mimeType": mime}))
        return types.CallToolResult(content=content, is_error=res.is_error)

    server = Server("video-workbench", version=__version__,
                    instructions=f"Video Workbench toolkit for request {request_id}, role {role}.",
                    on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(request_id: str, role: str, agent: str) -> None:
    anyio.run(serve, request_id, role, agent)
