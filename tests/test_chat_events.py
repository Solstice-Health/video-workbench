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
from vwb.workspace.request import Request


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


def test_native_compaction_keeps_current_context_separate_from_billed_iterations(ws):
    key = "anthropic_api:claude-opus-5-5"
    events.set_active_host(ws, key)
    measured = {"input_tokens": 100000, "output_tokens": 4000, "total_tokens": 104000,
                "input_tokens_details": {"cached_tokens": 90000},
                "output_tokens_details": {"thinking_tokens": 3000},
                "billed_input_tokens": 900000, "billed_output_tokens": 5000,
                "billed_cached_input_tokens": 500000}
    limits = {"max_output_tokens": 128000, "auto_compaction_enabled": True, "auto_compact_tokens": 850000,
              "compaction_count": 1, "last_stop_reason": "tool_use"}
    write_json(ws.memory / "provider_context.json", {"key": key, "used_tokens": 104000,
        "limit_tokens": 1000000, "breakdown": measured, **limits})
    append_jsonl(ws.ledger / "tools.jsonl", {"usage": measured})

    usage = events.context_usage(ws)
    context, billing = usage["context"], usage["billing"]
    assert context["used_tokens"] == 104000 and context["percent"] == 10.4
    assert context["breakdown"]["input_tokens"] == 100000
    assert context["breakdown"]["cached_input_tokens"] == 90000
    assert context["breakdown"]["reasoning_output_tokens"] == 3000
    assert {field: context[field] for field in limits} == limits
    assert billing["calls"] == 1 and billing["input_tokens"] == 900000
    assert billing["output_tokens"] == 5000 and billing["total_tokens"] == 905000
    assert billing["cached_input_tokens"] == 500000 and billing["reasoning_output_tokens"] == 3000
    assert usage["compact_available"] is False

    events.set_active_host(ws, "openrouter_api:other")
    assert all(events.context_usage(ws)["context"][field] is None for field in limits)


def test_reported_zero_reasoning_and_billed_counters_do_not_fall_back_to_context(ws):
    append_jsonl(ws.ledger / "tools.jsonl", {"usage": {"input_tokens": 100, "output_tokens": 20,
        "output_tokens_details": {"reasoning_tokens": 0, "thinking_tokens": 15}}})
    append_jsonl(ws.ledger / "tools.jsonl", {"usage": {"input_tokens": 100, "output_tokens": 20,
        "billed_input_tokens": 0, "billed_output_tokens": 0, "billed_cached_input_tokens": 0,
        "billed_reasoning_output_tokens": 0, "output_tokens_details": {"thinking_tokens": 15}}})
    usage = events.context_usage(ws)
    assert usage["billing"]["reasoning_output_tokens"] == 0
    assert usage["billing"]["total_tokens"] == 120
    assert usage["billing"]["input_tokens"] == 100 and usage["billing"]["output_tokens"] == 20


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
    ws.save(Request(id='r-events', message='Synthetic event test'))
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
    ws.save(Request(id='r-events', message='Synthetic event test'))
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


def _activity(ws, rows):
    path = ws.memory / events.EVENT_FILE
    with path.open("a") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def _event(seq, ident=None, kind="thinking", actor="job_lead", **data):
    return {"seq": seq, "ts": f"2026-10-21T12:{seq // 60:02}:{seq % 60:02}Z", "kind": kind,
            "actor": actor, "data": {**({"id": ident} if ident is not None else {}), **data}}


def test_activity_pages_coalesce_snapshots_by_kind_actor_and_identity(ws):
    _activity(ws, [_event(seq, "stream", text="x" * seq, status="in_progress") for seq in range(1, 1001)])
    _activity(ws, [_event(1001, "stream", text="Final thought", status="completed"),
                   _event(1002, "stream", kind="message", text="Answer"),
                   _event(1003, "stream", actor="child", text="Independent thought")])
    page = events.event_page(ws)
    assert [row["seq"] for row in page["events"]] == [1001, 1002, 1003]
    assert page["events"][0]["data"]["text"] == "Final thought"
    assert page["cursor"] == 1003 and not page["events_has_more"]
    assert len(events.read_events(ws)) == 1003  # Original stream remains intact.


def test_older_activity_pagination_is_bounded_chronological_and_complete(ws):
    _activity(ws, [_event(seq, f"tool-{seq}", kind="tool", output=f"Result {seq}") for seq in range(1, 21)])
    page = events.event_page(ws, limit=5)
    assert [row["seq"] for row in page["events"]] == list(range(16, 21))
    collected = page["events"]
    while page["events_has_more"]:
        page = events.event_page(ws, before=page["events_before"], limit=5)
        assert page["cursor"] == 20
        assert len(page["events"]) <= 5
        collected = page["events"] + collected
    assert [row["seq"] for row in collected] == list(range(1, 21))


def test_forward_activity_catchup_never_advances_past_undelivered_results(ws):
    _activity(ws, [_event(seq, f"tool-{seq}", kind="tool", output="x" * 1000) for seq in range(1, 11)])
    cursor, collected = 0, []
    while True:
        page = events.event_page(ws, after=cursor, limit=8, byte_limit=2400)
        assert len(page["events"]) <= 2
        collected.extend(page["events"])
        cursor = page["cursor"]
        if not page["events_after_has_more"]:
            break
        assert cursor == collected[-1]["seq"]
    assert [row["seq"] for row in collected] == list(range(1, 11))
    assert cursor == 10
    assert events.event_page(ws, after=cursor)["events"] == []


def test_warm_activity_poll_only_parses_new_bytes_and_delivered_rows(ws, monkeypatch):
    _activity(ws, [_event(seq, "stream", text="Saved snapshot") for seq in range(1, 1001)])
    cursor = events.event_page(ws)["cursor"]
    real_loads = events.json.loads
    parsed = []
    def loads(value, *args, **kwargs):
        row = real_loads(value, *args, **kwargs)
        if isinstance(row, dict) and "seq" in row:
            parsed.append(row["seq"])
        return row
    monkeypatch.setattr(events.json, "loads", loads)
    assert events.event_page(ws, after=cursor)["events"] == []
    assert not parsed
    _activity(ws, [_event(1001, "stream", text="Finished", status="completed")])
    page = events.event_page(ws, after=cursor)
    assert [row["seq"] for row in page["events"]] == [1001]
    assert parsed == [1001, 1001]
    assert len(events.read_events(ws, after=1000)) == 1
    assert all(seq > cursor for seq in parsed)


def test_activity_index_retries_partial_lines_and_rebuilds_after_replacement(ws):
    path = _activity(ws, [])
    assert events.event_page(ws)["cursor"] == 0
    row = json.dumps(_event(1, "first", text="Complete only when newline exists"))
    with path.open("a") as handle:
        handle.write(row[:40])
    assert not events.event_page(ws)["events"]
    with path.open("a") as handle:
        handle.write(row[40:] + "\n")
    assert events.event_page(ws)["cursor"] == 1
    replacement = path.with_suffix(".replacement")
    replacement.write_text(json.dumps(_event(1, "replacement", text="New file")) + "\n")
    replacement.replace(path)
    assert events.event_page(ws)["events"][0]["data"]["text"] == "New file"
    path.write_text(json.dumps(_event(1, "truncated", text="Short")) + "\n")
    assert events.event_page(ws)["events"][0]["data"]["text"] == "Short"


def test_billing_poll_only_reads_new_complete_lines(ws, monkeypatch):
    path = ws.ledger / "tools.jsonl"
    append_jsonl(path, {"usage": {"input_tokens": 100, "output_tokens": 20}})
    assert events.context_usage(ws)["billing"]["total_tokens"] == 120
    real_loads = events.json.loads
    parsed = []
    def loads(value, *args, **kwargs):
        parsed.append(value)
        return real_loads(value, *args, **kwargs)
    monkeypatch.setattr(events.json, "loads", loads)
    assert events.context_usage(ws)["billing"]["total_tokens"] == 120
    assert not parsed
    line = json.dumps({"usage": {"input_tokens": 50, "output_tokens": 10}})
    with path.open("a") as handle:
        handle.write(line[:20])
    assert events.context_usage(ws)["billing"]["total_tokens"] == 120
    with path.open("a") as handle:
        handle.write(line[20:] + "\n")
    assert events.context_usage(ws)["billing"]["total_tokens"] == 180
    assert len(parsed) == 1


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 301}, {"before": 0}, {"after": -1}, {"after": 1, "before": 2}])
def test_activity_page_refuses_ambiguous_or_invalid_cursors(ws, kwargs):
    with pytest.raises(ValueError):
        events.event_page(ws, **kwargs)
