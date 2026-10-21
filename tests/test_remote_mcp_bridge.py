"""Direct MCP stays inside the existing agent policy, provenance and context path."""
from __future__ import annotations

import json

import pytest

from vwb.integrations import IntegrationError, IntegrationService
from vwb.integrations.artifacts import import_result
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace, references

SERVER = "mcp_012345abcdef"


class Remote:
    def __init__(self):
        self.calls = []

    def status(self):
        return [{"id": SERVER, "name": "Example", "generic": True, "kind": "mcp", "connected": True,
                 "state": "connected", "tools_count": 2, "tools": [{"name": "read", "enabled": True}],
                 "client_id": "public-client", "url": "https://example.com/mcp"}]

    def tools(self, identifier, **kwargs):
        assert identifier == SERVER
        return {"integration": identifier, "tools": [
            {"name": f"{SERVER}:read", "inputSchema": {"type": "object"}, "dispatch_tool": "integration_call"},
            {"name": f"{SERVER}:write", "inputSchema": {"type": "object"}, "dispatch_tool": "integration_execute"}]}

    def call(self, tool, arguments, *, read_only):
        if read_only and tool.endswith(":write"):
            raise IntegrationError("This tool is not read-only.")
        self.calls.append((tool, arguments, read_only))
        return {"result": {"content": [{"type": "text", "text": "Remote evidence"}]},
                "gateway": "https://example.com/mcp", "provider": SERVER}


@pytest.fixture
def bridge(tmp_path):
    service = IntegrationService(tmp_path / "host")
    service.remote = Remote()
    ws = Workspace(tmp_path / "r-test")
    ws.work.mkdir(parents=True)
    ws.ledger.mkdir()
    ctx = ToolContext(ws, "job_lead", "job_lead", None, state={"integration_service": service})
    return build_registry(), ctx, service


def test_discovery_and_read_import_use_dynamic_id(bridge):
    registry, ctx, service = bridge
    status = registry.call(ctx, "integration_status", {})
    item = next(row for row in status.data["items"] if row["id"] == SERVER)
    assert item["connected"] and "tools" not in item and "client_id" not in item
    catalog = registry.call(ctx, "integration_tools", {"integration": SERVER})
    assert catalog.data["tools"][0]["name"] == f"{SERVER}:read"
    result = registry.call(ctx, "integration_call", {"tool": f"{SERVER}:read", "arguments": {}})
    assert not result.is_error and service.remote.calls[-1][2] is True
    path = ctx.ws.work / result.data["artifacts"][0]["path"]
    assert path.is_file() and f"integrations/{SERVER}/" in str(path)
    assert json.loads(path.read_text())["tool"] == f"{SERVER}:read"
    events = [json.loads(row) for row in (ctx.ws.ledger / "ledger.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "integration.imported"
    assert events[-1]["data"]["integration"] == SERVER


def test_execute_has_independent_policy_role_and_inherited_permissions(bridge, monkeypatch):
    registry, ctx, service = bridge
    args = {"tool": f"{SERVER}:write", "arguments": {}}
    assert registry.call(ctx, "integration_call", args).is_error
    assert not service.remote.calls
    monkeypatch.setattr(registry, "mode", lambda capability: "block" if capability == "integration.execute" else "allow")
    assert registry.call(ctx, "integration_execute", args).is_error
    assert not service.remote.calls
    monkeypatch.setattr(registry, "mode", lambda capability: "allow")
    ctx.role = "critic"
    assert registry.call(ctx, "integration_execute", args).is_error
    ctx.role = "job_lead"
    ctx.state["allowed_tools"] = {"integration_status", "integration_call"}
    assert registry.call(ctx, "integration_execute", args).is_error
    ctx.state.pop("allowed_tools")
    result = registry.call(ctx, "integration_execute", args)
    assert not result.is_error and service.remote.calls == [(f"{SERVER}:write", {}, False)]
    assert "integration_execute" in (ctx.ws.ledger / "tools.jsonl").read_text()


def test_direct_selection_survives_as_reference_not_credentials():
    value = references.validate({"plugins": [SERVER]})
    assert value["plugins"] == [SERVER]
    prompt = references.prompt(value)
    assert SERVER in prompt and "integration_tools" in prompt and "dispatch_tool" in prompt
    with pytest.raises(ValueError):
        references.validate({"plugins": ["mcp_../private"]})


def test_provider_identifier_cannot_escape_import_folder(bridge):
    _, ctx, _ = bridge
    with pytest.raises(IntegrationError, match="provider"):
        import_result(ctx.ws, "read", {}, {"content": []}, "https://example.com/mcp",
                      provider="mcp_../../private")
