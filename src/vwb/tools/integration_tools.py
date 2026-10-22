"""Remote integrations through Registry with separate read and execution policies."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from vwb.integrations import IntegrationError, IntegrationService
from vwb.tools.base import Tool, ToolContext, ToolResult, schema


def _service(ctx: ToolContext) -> IntegrationService:
    return ctx.state.get("integration_service") or IntegrationService()


def integration_status(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if arguments:
        return ToolResult.error("integration_status takes no arguments.")
    try:
        status = _service(ctx).status()
        # Agent gets capabilities/status, not host public-client configuration or OAuth state.
        items = [{k: v for k, v in item.items() if k not in {"tools", "client_id", "scope", "url", "auth", "transport"}}
                 for item in status["items"]]
        return ToolResult.json({"items": items, "notice": "Connect directly to an MCP server or use a provider preset in Workbench Integrations. Credentials stay on the host. Discover tools using the item's exact id."})
    except IntegrationError as exc:
        return ToolResult.error(str(exc))


CATALOG_SCHEMA = schema({"integration": {"type": "string", "maxLength": 128, "default": "solstice",
                                        "description": "An exact integration id from integration_status, including saved mcp_* servers."},
                         "query": {"type": "string", "maxLength": 1000},
                         "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                         "offset": {"type": "integer", "minimum": 0}})
CALL_SCHEMA = schema({"tool": {"type": "string", "minLength": 1, "maxLength": 256},
                      "arguments": {"type": "object"}}, ["tool", "arguments"])


def _agent_result(result: dict) -> dict:
    """Bound both text and structured data; the complete import remains on disk."""
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized.encode()) <= 48_000:
        return result
    full = next(item["path"] for item in result["artifacts"] if item["path"].endswith("/response.json"))
    # Artifact names are host-generated ASCII and hashes are fixed length. At most
    # 201 compact rows plus this 12 KiB excerpt stay below the 64 KiB result budget.
    artifacts = [{key: item[key] for key in ("path", "sha256")} for item in result["artifacts"]]
    evidence = json.dumps({"content": result.get("content"), "structuredContent": result.get("structuredContent")}, ensure_ascii=False)
    excerpt = evidence.encode()[:12_000].decode("utf-8", errors="ignore")
    return {"schema": result["schema"], "gateway": result["gateway"], "tool": result["tool"],
            "retrieved_at": result["retrieved_at"], "isError": result["isError"],
            "content_truncated": True, "full_response_path": full, "artifacts": artifacts,
            "excerpt": excerpt,
            "notice": "The complete external response is saved in the work file full_response_path. Read it in slices with file_read; narrow node IDs/depth for the next call. Imported evidence is not approved copy or an accepted canvas."}


def integration_tools(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    try:
        Draft202012Validator(CATALOG_SCHEMA).validate(arguments)
        result = _service(ctx).tools(**arguments)
        if arguments.get("integration") in {"blender_mcp", "after_effects"}:
            # Creative apps run on the host, not at the agent container's /work.
            # Give scripts an exact destination without exposing host credentials.
            result["workspace_path"] = str(ctx.ws.work.resolve())
        return ToolResult.json(result)
    except ValidationError:
        return ToolResult.error("Invalid integration_tools arguments. Use the published schema.")
    except IntegrationError as exc:
        return ToolResult.error(str(exc))


def integration_call(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _call(ctx, arguments, execute=False)


def integration_execute(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _call(ctx, arguments, execute=True)


def _call(ctx: ToolContext, arguments: dict[str, Any], *, execute: bool) -> ToolResult:
    try:
        Draft202012Validator(CALL_SCHEMA).validate(arguments)
        service = _service(ctx)
        handler = service.execute if execute else service.call
        result = handler(ctx.ws, arguments["tool"], arguments["arguments"])
        images = result.pop("images", [])
        value = ToolResult.json(_agent_result(result), images=images, artifacts=[item["sha256"] for item in result["artifacts"]])
        value.is_error = bool(result["isError"])
        return value
    except ValidationError:
        return ToolResult.error("Invalid integration tool arguments. Use the published schema.")
    except IntegrationError as exc:
        return ToolResult.error(str(exc))


TOOLS = [
    Tool("integration_status", "Check saved direct MCP servers and provider presets for connection and tool availability. Does not authenticate or make remote calls.",
         schema({}), integration_status, "integration.read", "integrations"),
    Tool("integration_tools", "Discover enabled tools and exact schemas using an integration id from integration_status. Direct MCP tools include dispatch_tool: integration_call for reads, integration_execute for other explicitly enabled tools. Presets include blender_mcp, figma_mcp, figma_direct, figma and solstice. After Effects uses its native after_effects_* tools.",
         CATALOG_SCHEMA, integration_tools, "integration.read", "integrations"),
    Tool("integration_call", "Call one discovered, enabled read tool. Namespaced mcp_* tools connect directly to their saved server. Images/documents import with provenance and raster asset_ref values into work/integrations. Other direct tools require integration_execute.",
         CALL_SCHEMA, integration_call, "integration.read", "integrations"),
    Tool("integration_execute", "Execute a discovered direct MCP tool explicitly enabled by the user in Integrations. It may change remote data. Follow the user's request and tool schema; enabling a tool does not authorize unrelated actions. Calls are never automatically retried after dispatch.",
         CALL_SCHEMA, integration_execute, "integration.execute", "integrations"),
]
