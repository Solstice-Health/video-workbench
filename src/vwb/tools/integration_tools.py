"""Read-only remote integrations through the same Registry policy as local tools."""

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
        return ToolResult.json({"items": status["items"], "notice": "Connect through the Workbench Integrations panel. Credentials stay on the host."})
    except IntegrationError as exc:
        return ToolResult.error(str(exc))


CATALOG_SCHEMA = schema({"integration": {"type": "string", "enum": ["solstice", "figma"], "default": "solstice"},
                         "query": {"type": "string", "maxLength": 1000},
                         "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                         "offset": {"type": "integer", "minimum": 0}})
CALL_SCHEMA = schema({"tool": {"type": "string", "minLength": 1, "maxLength": 128},
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
        return ToolResult.json(_service(ctx).tools(**arguments))
    except ValidationError:
        return ToolResult.error("Invalid integration_tools arguments. Use the published schema.")
    except IntegrationError as exc:
        return ToolResult.error(str(exc))


def integration_call(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    try:
        Draft202012Validator(CALL_SCHEMA).validate(arguments)
        result = _service(ctx).call(ctx.ws, arguments["tool"], arguments["arguments"])
        images = result.pop("images", [])
        value = ToolResult.json(_agent_result(result), images=images, artifacts=[item["sha256"] for item in result["artifacts"]])
        value.is_error = bool(result["isError"])
        return value
    except ValidationError:
        return ToolResult.error("Invalid integration_call arguments. Use the published schema.")
    except IntegrationError as exc:
        return ToolResult.error(str(exc))


TOOLS = [
    Tool("integration_status", "Check Solstice/Figma connection and read-tool availability. Does not authenticate or make remote calls.",
         schema({}), integration_status, "integration.read", "integrations"),
    Tool("integration_tools", "Discover approved read tools and exact argument schemas on the connected Solstice gateway. Filter integration='figma' for Figma; no direct Figma calls.",
         CATALOG_SCHEMA, integration_tools, "integration.read", "integrations"),
    Tool("integration_call", "Call one discovered, approved read tool through Solstice. Embedded images/documents are imported with provenance into work/integrations. Never follows advertised sibling servers or mutates remote state.",
         CALL_SCHEMA, integration_call, "integration.read", "integrations"),
]
