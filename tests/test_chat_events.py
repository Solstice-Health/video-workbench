"""Public activity, token accounting and native compaction contracts, without model calls."""
from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from filelock import FileLock

from vwb.agents import events, host_codex, job_lead
from vwb.util.jsonio import append_jsonl, read_json, write_json
from vwb.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.memory.mkdir()
    workspace.ledger.mkdir()
    return workspace


def test_event_sequence_survives_concurrent_writes_and_partial_tail(ws):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: events.emit(ws, "test", {"i": i}), range(20)))
    rows = events.read_events(ws)
    assert [row["seq"] for row in rows] == list(range(1, 21))
    assert len(events.read_events(ws, after=16)) == 4
    with (ws.memory / events.EVENT_FILE).open("a") as fh:
        fh.write('{"seq":21')
    assert len(events.read_events(ws)) == 20


def test_codex_tool_snapshots_and_reasoning_exclude_opaque_payloads(ws):
    events.codex_event(ws, {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
        "command": "python build.py", "status": "in_progress"}}, "attempt1")
    events.codex_event(ws, {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
        "command": "python build.py", "aggregated_output": "Rendered 900 frames", "exit_code": 0,
        "status": "completed"}}, "attempt1")
    events.codex_event(ws, {"type": "item.completed", "item": {"id": "i2", "type": "reasoning",
        "text": "Checking the source timing.", "encrypted_content": "PRIVATE"}}, "attempt1")
    events.codex_event(ws, {"type": "item.completed", "item": {"id": "i3", "type": "mcp_tool_call",
        "tool": "media_frame", "result": {"content": [{"type": "text", "text": "Frame 45"},
        {"type": "image", "data": "PRIVATE"}]}}}, "attempt1")
    rows = events.read_events(ws)
    assert rows[0]["data"]["id"] == rows[1]["data"]["id"] == "attempt1:i1"
    assert rows[1]["data"]["output"] == "Rendered 900 frames"
    assert rows[2]["kind"] == "thinking" and rows[2]["data"]["text"] == "Checking the source timing."
    assert rows[3]["data"]["output"] == "Frame 45"
    assert "PRIVATE" not in json.dumps(rows)


def _usage_row(used, total=900000):
    return {"timestamp": "2026-10-12T12:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
        "model_context_window": 1000000,
        "last_token_usage": {"input_tokens": used - 100, "cached_input_tokens": 5000, "output_tokens": 100,
                             "reasoning_output_tokens": 50, "total_tokens": used},
        "total_token_usage": {"total_tokens": total}}}}


def _session(ws):
    write_json(ws.memory / "lead_session.json", {"key": "codex:test", "threads": {"codex:test": {"thread_id": "thread-1"}}})
    path = ws.root / "agent" / "codex" / "sessions" / "2026" / "rollout-thread-1.jsonl"
    append_jsonl(path, _usage_row(20100))
    return path


def test_context_is_latest_measurement_not_cumulative_usage(ws):
    path = _session(ws)
    for _i in range(2):
        append_jsonl(ws.ledger / "tools.jsonl", {"usage": {"input_tokens": 60000, "output_tokens": 500,
            "input_tokens_details": {"cached_tokens": 40000}, "output_tokens_details": {"reasoning_tokens": 400},
            "total_tokens": 60500}})
    usage = events.context_usage(ws)
    assert usage["context"]["used_tokens"] == 20100
    assert usage["context"]["percent"] == 2.01
    assert usage["billing"]["total_tokens"] == 121000
    assert usage["billing"]["cached_input_tokens"] == 80000
    assert usage["compact_available"] is True
    # Polling catches appended usage and retries partial rows without losing an update.
    line = json.dumps(_usage_row(10000))
    with path.open("a") as fh:
        fh.write(line[:40])
    assert events.context_usage(ws)["context"]["used_tokens"] == 20100
    with path.open("a") as fh:
        fh.write(line[40:] + "\n")
    assert events.context_usage(ws)["context"]["used_tokens"] == 10000


def test_context_unknown_never_guesses_and_fallback_does_not_show_codex_context(ws):
    usage = events.context_usage(ws)
    assert usage["context"]["percent"] is None
    assert usage["context"]["limit_tokens"] is None
    _session(ws)
    events.set_active_host(ws, "claude_sdk:other", "claude-thread")
    usage = events.context_usage(ws)
    assert usage["context"]["used_tokens"] is None
    assert usage["model"] == "other" and usage["compact_available"] is False


def _proc(rows):
    return SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO("".join(json.dumps(r) + "\n" for r in rows)))


def test_native_compaction_waits_for_successful_turn_not_rpc_ack():
    replies = [{"id": 1, "result": {}}, {"id": 2, "result": {}}, {"id": 3, "result": {}}]
    with pytest.raises(RuntimeError, match="exited before compaction completed"):
        host_codex._compact_rpc(_proc(replies), "thread-1", {"model": "test"})
    completed = {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"status": "completed"}}}
    proc = _proc(replies + [completed])
    host_codex._compact_rpc(proc, "thread-1", {"model": "test"})
    sent = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    assert [r["method"] for r in sent] == ["initialize", "initialized", "thread/resume", "thread/compact/start"]
    assert sent[-1]["params"] == {"threadId": "thread-1"}
    completed["params"]["turn"] = {"status": "failed", "error": {"message": "upstream failed"}}
    with pytest.raises(RuntimeError, match="upstream failed"):
        host_codex._compact_rpc(_proc(replies + [completed]), "thread-1", {"model": "test"})


def test_compact_preserves_thread_and_does_not_add_chat(ws, monkeypatch):
    _session(ws)
    original = read_json(ws.memory / "lead_session.json")
    calls = []
    monkeypatch.setattr(job_lead, "build_registry", lambda: "registry")
    monkeypatch.setattr(host_codex, "compact_thread", lambda *args: calls.append(args))
    result = job_lead.compact(ws)
    assert result["mode"] == "native" and result["status"] == "completed"
    assert calls[0][2] == "thread-1"
    assert read_json(ws.memory / "lead_session.json") == original
    assert not ws.request_path.exists()
    assert [r["kind"] for r in events.read_events(ws)] == ["compaction.started", "compaction.completed"]
    with FileLock(str(ws.root / "turn.lock")):
        with pytest.raises(RuntimeError, match="Queue compaction"):
            job_lead.compact(ws)


def test_interrupt_keeps_thread_and_does_not_retry(ws, monkeypatch):
    original = {"threads": {"codex:test": {"thread_id": "thread-1"}}}
    write_json(ws.memory / "lead_session.json", original)
    shutdown = []
    monkeypatch.setattr(job_lead, "build_registry", lambda: "registry")
    monkeypatch.setattr(job_lead, "Swarm", lambda *_: SimpleNamespace(shutdown=lambda: shutdown.append(True)))

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(job_lead, "_lead", interrupted)
    with pytest.raises(KeyboardInterrupt):
        job_lead.run(ws, "new direction", False)
    assert read_json(ws.memory / "lead_session.json") == original
    assert shutdown == [True]
    assert [r["kind"] for r in events.read_events(ws)] == ["turn.started", "turn.stopped"]


def test_second_interrupt_during_worker_cleanup_keeps_new_thread(ws, monkeypatch):
    def shutdown():
        raise KeyboardInterrupt

    def interrupted(*args, **kwargs):
        session = args[7]
        session["threads"] = {"codex:test": {"thread_id": "new-thread"}}
        raise KeyboardInterrupt

    monkeypatch.setattr(job_lead, "build_registry", lambda: "registry")
    monkeypatch.setattr(job_lead, "Swarm", lambda *_: SimpleNamespace(shutdown=shutdown))
    monkeypatch.setattr(job_lead, "_lead", interrupted)
    with pytest.raises(KeyboardInterrupt):
        job_lead.run(ws, "make a short", True)
    assert read_json(ws.memory / "lead_session.json")["threads"]["codex:test"]["thread_id"] == "new-thread"


def test_historical_backfill_is_timestamped_public_and_only_once(ws):
    path = ws.root / "agent" / "codex" / "sessions" / "rollout-history.jsonl"
    def record(payload, second):
        append_jsonl(path, {"timestamp": f"2026-10-12T12:00:0{second}Z", "type": "response_item", "payload": payload})
    record({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "PRIVATE"}]}, 0)
    record({"type": "reasoning", "summary": [], "encrypted_content": "PRIVATE"}, 1)
    record({"type": "custom_tool_call", "call_id": "call1", "name": "exec", "input": "text('hello')"}, 2)
    record({"type": "custom_tool_call_output", "call_id": "call1", "output": [
        {"type": "input_text", "text": "hello"}, {"type": "image", "data": "PRIVATE"}]}, 3)
    record({"type": "reasoning", "id": "reason1", "summary": [{"type": "summary_text", "text": "Checking output"}]}, 4)
    rows = events.read_events(ws)
    assert len(rows) == 3
    assert rows[0]["ts"] == "2026-10-12T12:00:02Z"
    assert rows[0]["data"]["id"] == rows[1]["data"]["id"]
    assert rows[1]["data"]["output"] == "hello"
    assert rows[2]["kind"] == "thinking"
    assert "PRIVATE" not in json.dumps(rows)
    assert events.read_events(ws) == rows
    new = events.emit(ws, "turn.started", {})
    assert new["seq"] == 4


def test_ui_container_labels_are_scoped_to_valid_job_id(monkeypatch):
    monkeypatch.delenv("VWB_UI_JOB_ID", raising=False)
    assert host_codex._ui_job_labels() == []
    monkeypatch.setenv("VWB_UI_JOB_ID", "anything")
    assert host_codex._ui_job_labels() == []
    job_id = "0123456789abcdef" * 2
    monkeypatch.setenv("VWB_UI_JOB_ID", job_id)
    assert host_codex._ui_job_labels() == ["--label", f"vwb.ui-job={job_id}"]
