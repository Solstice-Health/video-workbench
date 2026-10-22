"""Creative application presets retain Registry authority and durable provenance."""
from __future__ import annotations

import json

import pytest

from vwb.integrations import IntegrationError, IntegrationService
from vwb.integrations.artifacts import import_result
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace, references


class BlenderFixture:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"id": "blender_mcp", "kind": "mcp", "name": "Blender MCP", "connected": True,
                "state": "connected", "available": True, "tools_count": 2}

    def tools(self, **kwargs):
        return {"integration": "blender_mcp", "tools": [
            {"name": "blender_mcp.get_scene_info", "dispatch_tool": "integration_call"},
            {"name": "blender_mcp.execute_blender_code", "dispatch_tool": "integration_execute"}]}

    def call(self, ws, tool, args, *, read_only):
        if read_only and tool.endswith("execute_blender_code"):
            raise IntegrationError("Use integration_execute for Blender code.")
        self.calls.append((tool, args, read_only))
        return import_result(ws, tool, args, {"content": [{"type": "text", "text": "Scene fixture"}]},
                             "stdio:blender", provider="blender_mcp")


@pytest.fixture
def creative(tmp_path):
    service = IntegrationService(tmp_path / "host")
    service.blender = BlenderFixture()
    ws = Workspace(tmp_path / "r-creative")
    ws.work.mkdir(parents=True)
    ws.ledger.mkdir()
    ctx = ToolContext(ws, "job_lead", "job_lead", None, state={"integration_service": service})
    return build_registry(), ctx, service


def test_blender_discovery_and_evidence_stay_in_registry(creative):
    registry, ctx, service = creative
    catalog = registry.call(ctx, "integration_tools", {"integration": "blender_mcp"})
    assert catalog.data["tools"][0]["dispatch_tool"] == "integration_call"
    result = registry.call(ctx, "integration_call", {"tool": "blender_mcp.get_scene_info", "arguments": {}})
    assert not result.is_error
    assert service.blender.calls == [("blender_mcp.get_scene_info", {}, True)]
    response = ctx.ws.work / next(a["path"] for a in result.data["artifacts"] if a["path"].endswith("response.json"))
    assert json.loads(response.read_text())["tool"] == "blender_mcp.get_scene_info"
    assert "integration.imported" in (ctx.ws.ledger / "ledger.jsonl").read_text()


def test_blender_execution_requires_execute_policy_and_role(creative, monkeypatch):
    registry, ctx, service = creative
    args = {"tool": "blender_mcp.execute_blender_code", "arguments": {"code": "pass"}}
    assert registry.call(ctx, "integration_call", args).is_error
    assert service.blender.calls == []
    monkeypatch.setattr(registry, "mode", lambda cap: "block" if cap == "integration.execute" else "allow")
    assert registry.call(ctx, "integration_execute", args).is_error
    monkeypatch.setattr(registry, "mode", lambda cap: "allow")
    ctx.role = "critic"
    assert registry.call(ctx, "integration_execute", args).is_error
    ctx.role = "job_lead"
    assert not registry.call(ctx, "integration_execute", args).is_error
    assert service.blender.calls == [(args["tool"], args["arguments"], False)]


def test_native_after_effects_is_discoverable_without_a_fake_mcp_server(creative):
    registry, ctx, _ = creative
    result = registry.call(ctx, "integration_tools", {"integration": "after_effects"})
    assert not result.is_error and result.data["tools"] == []
    assert "after_effects_run" in result.data["notice"]
    names = {tool.name for tool in registry.for_role("job_lead")}
    assert {"after_effects_status", "after_effects_run", "after_effects_render", "after_effects_job"} <= names
    assert "after_effects_run" not in {tool.name for tool in registry.for_role("critic")}


def test_creative_presets_survive_selected_chat_context():
    value = references.validate({"plugins": ["after_effects", "blender_mcp"]})
    prompt = references.prompt(value)
    assert value["plugins"] == ["after_effects", "blender_mcp"]
    assert "after_effects_run" in prompt and "integration_execute" in prompt
    assert "real application" in prompt and "not in Docker" in prompt
