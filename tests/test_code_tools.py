"""Provider-independent policy and evidence contracts without model calls."""
from __future__ import annotations

import json
import threading

import pytest

from vwb.agents import swarm
from vwb.code import runtime
from vwb.tools import build_registry, code_tools
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace


@pytest.fixture
def harness(tmp_path, monkeypatch):
    ws = Workspace(tmp_path)
    for directory in (ws.work, ws.ledger, ws.memory):
        directory.mkdir()
    calls = []
    def invoke(workspace, operation, args, *, cancel=None):
        calls.append((workspace, operation, args, cancel))
        return {"items": [{"path": "source.py", "line": 1, "sha256": "a" * 64}], "next_offset": None}
    monkeypatch.setattr(runtime, "invoke", invoke)
    return build_registry(), ToolContext(ws, "main", "job_lead", None), calls


@pytest.mark.parametrize("role", ["job_lead", "worker", "scene_miner", "planner_b", "composer", "critic", "code_explorer"])
def test_all_provider_roles_receive_common_navigation(harness, role):
    registry, ctx, calls = harness
    ctx.role = role
    assert {tool.name for tool in code_tools.TOOLS} <= {tool.name for tool in registry.for_role(role)}
    result = registry.call(ctx, "repo_search", {"pattern": "needle", "root": "project"})
    assert not result.is_error and result.data["items"][0]["sha256"] == "a" * 64
    assert calls[0][1:3] == ("search", {"pattern": "needle", "root": "project"})
    logs = [json.loads(line) for line in (ctx.ws.ledger / "tools.jsonl").read_text().splitlines()]
    assert logs[-1]["tool"] == "repo_search" and logs[-1]["ok"]


@pytest.mark.parametrize("args", [{"pattern": "x", "command": "anything"}, {"pattern": "x", "limit": True},
                                  {"pattern": "x", "timeout_s": 31}, {"root": "."}])
def test_invalid_search_never_dispatches(harness, args):
    registry, ctx, calls = harness
    assert registry.call(ctx, "repo_search", args).is_error
    assert not calls


def test_policy_inherited_permissions_and_cancellation_precede_dispatch(harness, monkeypatch):
    registry, ctx, calls = harness
    ctx.state["allowed_tools"] = {"repo_read"}
    assert registry.call(ctx, "repo_search", {"pattern": "x"}).is_error
    ctx.state.clear()
    cancel = threading.Event()
    cancel.set()
    ctx.state["cancel_event"] = cancel
    assert registry.call(ctx, "repo_search", {"pattern": "x"}).is_error
    ctx.state.clear()
    monkeypatch.setattr(registry, "mode", lambda _: "block")
    assert registry.call(ctx, "repo_search", {"pattern": "x"}).is_error
    assert not calls


def test_explorer_has_no_shell_or_writes_and_cannot_widen_child(harness):
    registry, ctx, _ = harness
    names = {t.name for t in registry.for_role("code_explorer")}
    assert not names & {"file_write", "file_edit", "sandbox_run", "model_request", "integration_call", "canvas_commit"}
    assert "code_explorer" not in swarm.CODEX_ROLES
    assert "code_explorer" in swarm.ROLE_SLOT
    tree = swarm.Swarm(ctx.ws, registry)
    # Permission intersection, without launching any model or worker.
    tree._allowed["limited"] = names
    tree.tickets["limited"] = {"parent": "job_lead"}
    assert registry.allowed_for_role("worker", "file_write")
    child_allowed = names & {t.name for t in registry.for_role("worker")}
    assert "repo_search" in child_allowed and "file_write" not in child_allowed


def test_current_file_hint_is_separate_and_hashed(harness):
    from vwb.code.editor import save_context
    registry, ctx, calls = harness
    (ctx.ws.work / "source.py").write_text("answer = 42\n")
    save_context(ctx.ws, {"path": "work/source.py"})
    result = registry.call(ctx, "repo_context", {})
    assert not result.is_error
    assert result.data["editor"]["path"] == "source.py"
    assert result.data["editor"]["trusted"] is False
    assert calls[0][2]["active_files"] == ["source.py"]


def test_missing_runtime_is_actionable_failure(harness, monkeypatch):
    registry, ctx, _ = harness
    def unavailable(*args, **kwargs):
        raise runtime.CodeServiceError("Start Docker and run python -m vwb.code.runtime install")
    monkeypatch.setattr(runtime, "invoke", unavailable)
    result = registry.call(ctx, "repo_lsp", {"path": "source.py", "operation": "definition", "line": 1, "column": 1})
    assert result.is_error and "runtime install" in result.text
