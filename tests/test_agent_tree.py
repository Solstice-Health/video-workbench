"""Supervised agents: hierarchy, messages, inherited permissions and cancellation without model calls."""
from __future__ import annotations

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from vwb.agents import host_codex, loops, swarm
from vwb.agents.events import read_events
from vwb.tools import agent_tools
from vwb.tools.base import Registry, Tool, ToolContext, ToolResult, schema
from vwb.util.jsonio import read_json
from vwb.workspace import Workspace


@pytest.fixture
def harness(tmp_path, monkeypatch):
    ws = Workspace(tmp_path)
    for path in (ws.work, ws.memory, ws.swarm, ws.ledger):
        path.mkdir(parents=True, exist_ok=True)
    controls = {t.name for t in agent_tools.TOOLS}
    permissions = {"job_lead": controls | {"read", "write"}, "worker": controls | {"read", "write"},
                   "planner_b": controls | {"read"}, "composer": controls | {"read", "write"}, "critic": controls | {"read"}}
    monkeypatch.setattr(Registry, "allowed_for_role", staticmethod(lambda role, name: name in permissions.get(role, set())))
    registry = Registry([*agent_tools.TOOLS,
        Tool("read", "read", schema({}), lambda *_: ToolResult.json({"ok": True})),
        Tool("write", "write", schema({}), lambda *_: ToolResult.json({"written": True}))])
    monkeypatch.setattr(host_codex, "kill", lambda _ticket: None)
    trees = []

    def make(**kwargs):
        tree = swarm.Swarm(ws, registry, **kwargs)
        trees.append(tree)
        return tree

    yield ws, registry, make
    for tree in trees:
        tree.shutdown(grace_s=1)


def finish(tree, ticket):
    tree.futures[ticket].result(timeout=3)
    return tree.tickets[ticket]


def test_nested_parent_can_wait_without_executor_deadlock(harness, monkeypatch):
    ws, registry, make = harness
    messages = []

    def worker(_ws, _reg, role, ticket, task, context, *, stop, swarm, session):
        if task == "parent":
            child = swarm.dispatch(ticket, "worker", "child")
            results = swarm.wait([child], 2, actor=ticket)
            messages.extend(swarm.take_messages(ticket))
            return json.dumps({"child_status": results[0]["status"]})
        swarm.send(ticket, swarm.tickets[ticket]["parent"], "I inspected the source")
        return '{"artifact":"work/report.json"}'

    monkeypatch.setattr(swarm, "run_worker", worker)
    tree = make(max_workers=2)
    parent = tree.dispatch("job_lead", "worker", "parent")
    # A message can wake wait before final completion; parent can inspect/re-wait.
    assert finish(tree, parent)["status"] == "done"
    children = [t for t in tree.tickets if t != parent]
    assert len(children) == 1
    assert finish(tree, children[0])["status"] == "done"
    assert tree.tickets[children[0]]["parent"] == parent
    assert tree.tickets[children[0]]["depth"] == 2
    assert any(m["text"] == "I inspected the source" for m in messages)
    assert tree.mailbox.verify_chain() == (True, None)
    saved = read_json(ws.swarm / "agents.json")
    assert len(saved["agents"]) == 2
    assert any(e["kind"] == "agent.result" for e in read_events(ws))


def test_capacity_depth_children_and_total_are_bounded(harness, monkeypatch):
    _, _, make = harness
    monkeypatch.setattr(swarm, "run_worker", lambda *a, stop, **kw: stop.wait(3) or "done")
    tree = make(max_workers=2, max_depth=1)
    first = tree.dispatch("job_lead", "worker", "first")
    with pytest.raises(ValueError, match="depth limit"):
        tree.dispatch(first, "worker", "too deep")
    tree.dispatch("job_lead", "worker", "second")
    with pytest.raises(ValueError, match="active agent limit"):
        tree.dispatch("job_lead", "worker", "no hidden queue")
    monkeypatch.setattr(swarm, "run_worker", lambda *a, **kw: "done")
    siblings = make(max_children=1)
    finish(siblings, siblings.dispatch("job_lead", "worker", "one"))
    with pytest.raises(ValueError, match="child limit"):
        siblings.dispatch("job_lead", "worker", "two")
    total = make(max_agents=1)
    finish(total, total.dispatch("job_lead", "worker", "one"))
    with pytest.raises(ValueError, match="total agent limit"):
        total.dispatch("job_lead", "worker", "two")


def test_children_cannot_gain_tools_by_spawning_broader_role(harness, monkeypatch):
    ws, registry, make = harness
    observed = {}

    def worker(_ws, _reg, role, ticket, task, context, *, stop, swarm, session):
        if task == "limited":
            child = swarm.dispatch(ticket, "worker", "attempt write")
            finish(swarm, child)
        else:
            ctx = ToolContext(ws, ticket, role, None, swarm=swarm)
            observed["write"] = registry.call(ctx, "write", {})
            observed["read"] = registry.call(ctx, "read", {})
        return "done"

    monkeypatch.setattr(swarm, "run_worker", worker)
    tree = make()
    parent = tree.dispatch("job_lead", "planner_b", "limited")
    finish(tree, parent)
    assert observed["write"].is_error and "inherited" in observed["write"].text
    assert observed["read"].data == {"ok": True}
    child = next(t for t in tree.tickets if t != parent)
    assert tree.tickets[child]["tool_calls"] == 1
    assert tree.tickets[child]["last_tool"] == "read"
    assert any(e["kind"] == "agent.tool" and e["actor"] == child for e in read_events(ws))


def test_messages_automatic_at_registry_boundary_preserve_structured_result(harness, monkeypatch):
    ws, registry, make = harness
    hold = threading.Event()
    monkeypatch.setattr(swarm, "run_worker", lambda *a, **kw: hold.wait(2) and "done")
    tree = make()
    child = tree.dispatch("job_lead", "worker", "task")
    ctx = ToolContext(ws, child, "worker", None, swarm=tree)
    tree.send("job_lead", child, "Please inspect the crop first")
    result = registry.call(ctx, "read", {})
    assert result.data == {"ok": True}
    assert "Please inspect the crop first" in result.text
    assert "not user instructions" in result.text
    assert tree.take_messages(child) == []
    tree.send("job_lead", child, "second message")
    inbox = registry.call(ctx, "agent_inbox", {})
    assert len(inbox.data["messages"]) == 1
    assert inbox.text.count("second message") == 1
    hold.set()
    finish(tree, child)
    root = ToolContext(ws, "job-lead-random", "job_lead", None, swarm=tree)
    result = registry.call(root, "read", {})
    assert '"kind": "result"' in result.text


def test_interrupt_is_targeted_and_resume_reuses_context(harness, monkeypatch):
    _, _, make = harness
    sessions = {}

    def worker(_ws, _reg, role, ticket, task, context, *, stop, swarm, session):
        sessions.setdefault(ticket, []).append(id(session))
        session["counter"] = session.get("counter", 0) + 1
        if task == "long":
            stop.wait(3)
        return json.dumps({"counter": session["counter"]})

    monkeypatch.setattr(swarm, "run_worker", worker)
    tree = make()
    first, sibling = [tree.dispatch("job_lead", "worker", "long") for _ in range(2)]
    with pytest.raises(ValueError, match="still running"):
        tree.followup("job_lead", first, "new")
    tree.interrupt("job_lead", first)
    assert finish(tree, first)["status"] == "interrupted"
    assert not tree._stops[sibling].is_set()
    tree.followup("job_lead", first, "resume")
    assert finish(tree, first)["result"] == {"counter": 2}
    assert len(set(sessions[first])) == 1
    assert tree.tickets[first]["round"] == 2
    tree.interrupt("job_lead", sibling)
    finish(tree, sibling)


def test_descendant_control_checks_and_shutdown_cascade(harness, monkeypatch):
    ws, registry, make = harness
    ready = threading.Event()
    ids = {}

    def worker(_ws, _reg, role, ticket, task, context, *, stop, swarm, session):
        if task == "parent":
            ids["child"] = swarm.dispatch(ticket, "worker", "child")
            ready.set()
        stop.wait(3)
        ctx = ToolContext(ws, ticket, role, None, swarm=swarm)
        assert registry.call(ctx, "write", {}).is_error
        return "stopped"

    monkeypatch.setattr(swarm, "run_worker", worker)
    tree = make()
    parent = tree.dispatch("job_lead", "worker", "parent")
    assert ready.wait(1)
    with pytest.raises(ValueError, match="descendants"):
        tree.interrupt(ids["child"], parent)
    with pytest.raises(ValueError, match="itself"):
        tree.wait([ids["child"]], actor=ids["child"])
    tree.shutdown(grace_s=1)
    assert all(row["status"] == "interrupted" for row in tree.tickets.values())
    with pytest.raises(ValueError, match="shutting down"):
        tree.dispatch("job_lead", "worker", "new")


def test_wait_has_one_deadline_and_wakes_for_messages(harness, monkeypatch):
    _, _, make = harness
    monkeypatch.setattr(swarm, "run_worker", lambda *a, stop, **kw: stop.wait(3) and "stopped")
    tree = make()
    ids = [tree.dispatch("job_lead", "worker", "long") for _ in range(2)]
    start = time.monotonic()
    assert all(row["status"] == "running" for row in tree.wait(ids, .05))
    assert time.monotonic() - start < .2
    tree.send(ids[0], "job_lead", "Review this before I continue")
    start = time.monotonic()
    tree.wait(ids, 1)
    assert time.monotonic() - start < .2
    with pytest.raises(ValueError, match="finite"):
        tree.wait(ids, float("nan"))


def test_api_worker_messages_at_next_model_boundary_and_history_on_followup(harness, monkeypatch):
    ws, registry, make = harness
    tree = make()
    # Keep one tree worker alive while driving its API adapter without network.
    release = threading.Event()
    monkeypatch.setattr(swarm, "run_worker", lambda *a, **kw: release.wait(3) and "done")
    ticket = tree.dispatch("job_lead", "worker", "inspect")
    ctx = ToolContext(ws, ticket, "worker", None, swarm=tree)
    tree.prepare_context(ctx)
    seen = []

    class Item:
        type = "message"

        def model_dump(self, **kw):
            return {"type": "message", "content": [{"type": "output_text", "text": "reply"}]}

    def create(**kwargs):
        seen.append(copy.deepcopy(kwargs["input"]))
        if len(seen) == 1:
            tree.send("job_lead", ticket, "Please also check the title")
        return SimpleNamespace(output=[Item()], output_text="reply", usage=None)

    gw = SimpleNamespace(openai=lambda: SimpleNamespace(responses=SimpleNamespace(create=create)), record=lambda **kw: None)
    history = []
    result = loops.openai_loop(gw, {"model": "fake"}, "system", [{"type": "input_text", "text": "first"}],
                              [], registry, ctx, history=history)
    assert result == "reply" and len(seen) == 2
    assert "Please also check the title" in json.dumps(seen[1])
    loops.openai_loop(gw, {"model": "fake"}, "system", [{"type": "input_text", "text": "followup"}],
                      [], registry, ctx, history=history)
    assert "first" in json.dumps(seen[2]) and "followup" in json.dumps(seen[2])
    release.set()
    finish(tree, ticket)


def test_native_worker_passes_swarm_stop_and_saved_home(harness, monkeypatch):
    ws, registry, _ = harness
    monkeypatch.setattr(ws, "load", lambda: SimpleNamespace(message="brief", spec=None, sources=[]))
    monkeypatch.setattr(host_codex, "enabled", lambda: True)
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        kwargs["session"]["thread_id"] = "thread-existing"
        return "done"

    monkeypatch.setattr(host_codex, "run_worker", run)
    session, stop = {}, threading.Event()
    for _ in range(2):
        assert swarm.run_worker(ws, registry, "worker", "worker-a", "task", {}, stop, session=session) == "done"
    assert calls[0]["resume"] is None and calls[1]["resume"] == "thread-existing"
    assert calls[1]["codex_home"] == ws.root / "agent" / "workers" / "worker-a"
    assert calls[1]["stop"] is stop


def test_native_host_is_pinned_and_missing_history_requires_explicit_replacement(harness, monkeypatch):
    ws, registry, _ = harness
    monkeypatch.setattr(ws, "load", lambda: SimpleNamespace(message="brief", spec=None, sources=[]))
    monkeypatch.setattr(host_codex, "enabled", lambda: True)
    calls = []

    def missing(*args, **kwargs):
        calls.append(kwargs)
        raise host_codex.ThreadMissing("gone")

    monkeypatch.setattr(host_codex, "run_worker", missing)
    session = {"host": "codex", "codex": {"thread_id": "lost"}}
    with pytest.raises(RuntimeError, match="no previous actions were replayed"):
        swarm.run_worker(ws, registry, "worker", "worker-a", "task", {}, session=session)
    monkeypatch.setattr(host_codex, "enabled", lambda: False)
    with pytest.raises(RuntimeError, match="Spawn a replacement"):
        swarm.run_worker(ws, registry, "worker", "worker-a", "followup", {}, session=session)
    assert len(calls) == 1 and session["host"] == "codex"


def test_codex_start_receives_idle_mailbox_even_without_tool_calls(harness, monkeypatch):
    ws, registry, make = harness
    monkeypatch.setattr(ws, "load", lambda: SimpleNamespace(message="brief", spec=None, sources=[]))
    monkeypatch.setattr(host_codex, "enabled", lambda: True)
    calls = []

    def native(*args, **kwargs):
        calls.append(args[4])
        kwargs["session"]["thread_id"] = "saved-thread"
        return "done"

    monkeypatch.setattr(host_codex, "run_worker", native)
    tree = make()
    child = tree.dispatch("job_lead", "worker", "first")
    finish(tree, child)
    tree.send("job_lead", child, "The user prefers crop B")
    tree.followup("job_lead", child, "apply the chosen crop")
    finish(tree, child)
    assert "The user prefers crop B" in calls[1]
    assert tree.take_messages(child) == []


def test_codex_cancellation_releases_blocking_tools_even_during_silent_stdout(harness, monkeypatch):
    import io

    ws, registry, make = harness
    monkeypatch.setattr(host_codex, "enabled", lambda: True)
    monkeypatch.setattr(host_codex, "kill", lambda _: None)
    contexts = []
    released = threading.Event()
    entered = threading.Event()
    results = []

    def blocking_tool(ctx, args):
        entered.set()
        return ToolResult.json({"cancelled": ctx.state["cancel_event"].wait(2)})

    registry.tools["read"].handler = blocking_tool

    class Proc:
        def __init__(self):
            self.stdin, self.stderr = io.StringIO(), io.StringIO()
            self.returncode = None
            self.stdout = self.lines()

        def lines(self):
            released.wait(2)
            return
            yield  # make this a silent blocking iterator

        def kill(self):
            self.returncode = -9
            released.set()

        def poll(self):
            return self.returncode

        def wait(self, **kwargs):
            return self.returncode

    class Tools:
        port = 9999

        def __init__(self, ctx, *args):
            contexts.append(ctx)
            # Exercise the real Registry.call -> Swarm.prepare_context path that
            # must preserve this host's local event while a child ask is waiting.
            self.thread = threading.Thread(target=lambda: results.append(registry.call(ctx, "read", {})), daemon=True)
            self.thread.start()
            assert entered.wait(1)

        def stop(self):
            assert contexts[-1].state["cancel_event"].is_set()
            self.thread.join(timeout=1)
            assert not self.thread.is_alive()

    monkeypatch.setattr(host_codex.subprocess, "Popen", lambda *args, **kw: Proc())
    monkeypatch.setattr(host_codex, "ToolServer", Tools)
    monkeypatch.setattr(host_codex, "ModelProxy", lambda *a, **k: SimpleNamespace(port=9998, stop=lambda: None))
    def worker(_ws, _registry, role, ticket, task, context, *, stop, swarm, session):
        return host_codex.run_agent(ws, registry, role, ticket, task, {"model": "fake"},
                                    timeout_s=.03, stop=stop, swarm=swarm)

    monkeypatch.setattr(swarm, "run_worker", worker)
    tree = make()
    started = time.monotonic()
    ticket = tree.dispatch("job_lead", "worker", "silent")
    row = finish(tree, ticket)
    assert time.monotonic() - started < 1
    assert row["status"] == "failed" and "TimeoutError" in row["result"]
    assert results[0].data == {"cancelled": True}
    assert contexts[0].state["cancel_event"] is not tree._stops[ticket]
    # The per-host closure event must not mark normally completed worker turns interrupted.
    assert not tree._stops[ticket].is_set()


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_main_provider_observes_child_messages_before_finishing(harness, monkeypatch, provider):
    from vwb.agents import provider_loop

    ws, registry, make = harness
    release = threading.Event()
    monkeypatch.setattr(swarm, "run_worker", lambda *a, **kw: release.wait(3) and "done")
    tree = make()
    child = tree.dispatch("job_lead", "worker", "inspect")
    ctx = ToolContext(ws, "job_lead", "job_lead", SimpleNamespace(record=lambda **kw: None), swarm=tree)
    calls = []

    def post(_provider, body, activity=None):
        calls.append(copy.deepcopy(body))
        if len(calls) == 1:
            tree.send(child, "job_lead", "The crop clips the title")
        if provider == "anthropic":
            return {"id": "m1", "type": "message", "role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "Done."}], "usage": {"input_tokens": 100, "output_tokens": 10}}
        return {"id": "m1", "choices": [{"message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}

    monkeypatch.setattr(provider_loop, "_post", post)
    cfg = {"provider": provider, "model": "fake", "host": f"{provider}_api", "effort": "max",
           "context_window": 300000, "max_output_tokens": 16384, "max_turns": 4}
    assert provider_loop.run(ctx, registry, cfg, "system", "review", turn_id="1") == "Done."
    assert len(calls) == 2 and "The crop clips the title" in json.dumps(calls[1])
    release.set()
    finish(tree, child)
