"""Question waits resume their existing turn and never manufacture an answer."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from vwb.ask.broker import Broker
from vwb.gateway import Gateway
from vwb.tools import build_registry
from vwb.tools.base import Registry, Tool, ToolContext, ToolResult, schema
from vwb.util.jsonio import read_json, write_json
from vwb.web.queue import TurnQueue
from vwb.web.server import Workbench
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "r-questions")
    for sub in SUBDIRS:
        (workspace.root / sub).mkdir(parents=True)
    workspace.save(Request(id=workspace.id, message="Review choices"))
    monkeypatch.setattr("vwb.workspace.ledger.control_key", lambda: b"test-key")
    return workspace


def ask(broker, **kwargs):
    return broker.ask(asker="job_lead", question="Use the close-up?", why="Framing",
                      options=[{"id": "yes", "label": "Close-up"}, {"id": "no", "label": "Wide"}],
                      recommended="yes", **kwargs)


def wait_for(predicate):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if result := predicate():
            return result
        time.sleep(.01)
    pytest.fail("wait lease did not appear")


def test_blocking_wait_resumes_with_actual_answer(ws):
    broker = Broker(ws)
    ticket = ask(broker, mode="blocking", deadline_s=0)
    with ThreadPoolExecutor() as pool:
        waiter = pool.submit(broker.wait, ticket["id"], asker="job_lead", timeout_s=3)
        wait_for(lambda: broker.active_waiters(broker.tickets()[0]))
        assert broker.tickets()[0]["status"] == "open"
        broker.answer(ticket["id"], "no", "Keep the whole shot")
        result = waiter.result(timeout=2)
    assert result["status"] == "answered"
    assert result["answer"]["option"] == "no"
    assert not broker.active_waiters(broker.tickets()[0])


def test_wait_timeout_cancel_and_permission_never_default(ws):
    broker = Broker(ws)
    ticket = ask(broker, kind="allow", on_timeout="use_recommended", deadline_s=0)
    assert broker.tickets()[0]["status"] == "open"
    result = broker.wait(ticket["id"], asker="job_lead", timeout_s=.01)
    assert result["wait_status"] == "timeout" and result["answer"] is None
    cancel = threading.Event()
    with ThreadPoolExecutor() as pool:
        waiter = pool.submit(broker.wait, ticket["id"], asker="child", timeout_s=20, cancel=cancel)
        wait_for(lambda: broker.active_waiters(broker.tickets()[0]))
        cancel.set()
        assert waiter.result(timeout=1)["wait_status"] == "cancelled"
    assert not broker.active_waiters(broker.tickets()[0])
    assert broker.tickets()[0]["status"] == "open"


def test_registry_ask_modes_and_later_wait(ws):
    registry = build_registry()
    ctx = ToolContext(ws, "job_lead", "job_lead", Gateway(ws))
    args = {"question": "Which crop?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "recommended": "a", "mode": "non_blocking"}
    ticket = registry.call(ctx, "human_ask", args).data
    assert ticket["status"] == "open"
    result = registry.call(ctx, "human_wait", {"ticket": ticket["id"], "timeout_s": .01})
    assert result.data["wait_status"] == "timeout"
    assert result.data["on_timeout"] == "keep_parked"
    assert registry.call(ctx, "human_wait", {"ticket": "../../request", "timeout_s": 0}).is_error


def test_live_wait_answer_does_not_interrupt_or_enqueue_another_turn(ws, monkeypatch):
    broker = Broker(ws)
    ticket = ask(broker, mode="blocking")
    queue = TurnQueue(ws.root.parent)
    app = Workbench(ws.root.parent, queue=queue)
    monkeypatch.setattr(queue, "snapshot", lambda _ws: {"active": {"kind": "external"}, "queue": []})
    monkeypatch.setattr(queue, "enqueue", lambda *a, **k: pytest.fail("answer must resume the waiting tool"))
    monkeypatch.setattr("vwb.web.server.models.explicit_runtime", lambda *a: pytest.fail("no new model turn"))
    try:
        with ThreadPoolExecutor() as pool:
            waiter = pool.submit(broker.wait, ticket["id"], asker="job_lead", timeout_s=3)
            wait_for(lambda: broker.active_waiters(broker.tickets()[0]))
            response = app.answer(ws, {"ticket": ticket["id"], "option": "no", "mode": "now"})
            assert response["job"] is None and response["delivery"] == "waiting_tool"
            assert waiter.result(timeout=2)["answer"]["delivery"] == "waiting_tool"
    finally:
        queue.close()


def test_expired_wait_lease_queues_answer_normally(ws, monkeypatch):
    broker = Broker(ws)
    ticket = ask(broker, mode="blocking")
    ticket["waiters"] = {"crashed": {"actor": "job_lead", "expires_at": time.time() - 1}}
    write_json(broker._path(ticket["id"]), ticket)
    queue = TurnQueue(ws.root.parent)
    queue.stop(ws)
    app = Workbench(ws.root.parent, queue=queue)
    monkeypatch.setattr("vwb.web.server.models.explicit_runtime", lambda *a: None)
    try:
        response = app.answer(ws, {"ticket": ticket["id"], "option": "yes"})
        assert response["job"]["status"] == "queued"
        assert "delivery" not in read_json(broker._path(ticket["id"]))["answer"]
    finally:
        queue.close()


def test_policy_ask_respects_actual_grant_and_deny_on_retry(ws, monkeypatch):
    calls = []
    registry = Registry([Tool("test_action", "Test permission", schema({}),
                             lambda ctx, args: calls.append(args) or ToolResult.json({"ok": True}), "test.capability")])
    monkeypatch.setattr(registry, "mode", lambda _: "ask")
    ctx = ToolContext(ws, "job_lead", "job_lead", Gateway(ws))
    first = registry.call(ctx, "test_action", {})
    assert first.data["parked"] and not calls
    broker = Broker(ws)
    broker.answer(first.data["ticket"], "grant")
    assert registry.call(ctx, "test_action", {}).data == {"ok": True}
    assert len(calls) == 1
    broker.answer(first.data["ticket"], "deny")
    assert registry.call(ctx, "test_action", {}).is_error
    assert len(calls) == 1
