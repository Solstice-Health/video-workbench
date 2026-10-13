"""Provider routing, durable native history, streamed public text and context budgets, without paid calls."""

from __future__ import annotations

import copy
import io
import json
import random
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from vwb.agents import events, job_lead
from vwb.agents import provider_loop as loop
from vwb.gateway import Gateway
from vwb.gateway.services import _served_tier_from_stream, _tier_body
from vwb.tools.base import Registry, Tool, ToolContext, ToolResult
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, ChatTurn, Request


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "r-test")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id="r-test", message="Create a short"))
    return ToolContext(ws, "job_lead", "job_lead", Gateway(ws, actor="job_lead"))


def cfg(provider="anthropic", **extra):
    return {"provider": provider, "model": "test-model", "host": f"{provider}_api", "effort": "max",
            "context_window": 300000, "native_context_window": 1000000, "max_output_tokens": 16384,
            "max_turns": 4, "service_tier": "default", "speed": "standard", **extra}


def anthropic(blocks=None, reason="end_turn", **extra):
    return {"id": "msg1", "type": "message", "role": "assistant", "stop_reason": reason,
            "content": blocks or [{"type": "text", "text": "Done."}],
            "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 40}, **extra}


def openrouter(message=None, reason="stop", **extra):
    return {"id": "or1", "choices": [{"message": message or {"role": "assistant", "content": "Done."},
                                       "finish_reason": reason}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}, **extra}


def registry(handler=None):
    return Registry([Tool("file_read", "Read a file", {"type": "object", "properties": {}},
                          handler or (lambda *_: ToolResult(text="Read content")))])


def fake_calls(monkeypatch, replies, recorded=None):
    queue = iter(replies)
    def post(provider, body, activity=None):
        if recorded is not None:
            recorded.append(copy.deepcopy(body))
        return next(queue)
    monkeypatch.setattr(loop, "_post", post)


def test_anthropic_persistent_tools_signed_thinking_and_context(ctx, monkeypatch):
    calls = []
    block = {"type": "thinking", "thinking": "Checking evidence", "signature": "SIGNED_PRIVATE"}
    fake_calls(monkeypatch, [anthropic([block, {"type": "tool_use", "id": "t1", "name": "file_read", "input": {}}],
                                      "tool_use"), anthropic()], calls)
    assert loop.run(ctx, registry(), cfg(), "System", "First", turn_id="1") == "Done."
    assert calls[1]["messages"][1]["content"][0] == block
    assert calls[1]["messages"][2]["content"][0]["tool_use_id"] == "t1"
    assert calls[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert calls[0]["output_config"] == {"effort": "max"}
    saved = read_json(loop.history_path(ctx, "anthropic", "test-model"))
    assert saved["finished_turn"] == "1" and len(saved["messages"]) == 4
    assert "SIGNED_PRIVATE" not in json.dumps(events.read_events(ctx.ws))
    usage = read_json(ctx.ws.memory / "provider_context.json")
    assert usage["used_tokens"] == 150 and usage["limit_tokens"] == 300000
    assert usage["breakdown"]["input_tokens_details"]["cached_tokens"] == 40
    fake_calls(monkeypatch, [anthropic()], calls)
    loop.run(ctx, registry(), cfg(), "System", "Second", turn_id="2")
    assert calls[-1]["messages"][0]["content"] == "First"
    assert calls[-1]["messages"][-1]["content"] == "Second"


def test_openrouter_replays_reasoning_and_tool_calls(ctx, monkeypatch):
    calls = []
    message = {"role": "assistant", "content": None,
               "reasoning_details": [{"type": "reasoning.encrypted", "data": "OPAQUE_PRIVATE", "id": "r1"},
                                     {"type": "reasoning.text", "text": "Reading the file"}],
               "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "file_read", "arguments": "{}"}}]}
    fake_calls(monkeypatch, [openrouter(message, "tool_calls"), openrouter()], calls)
    loop.run(ctx, registry(), cfg("openrouter", service_tier="fast"), "System", "Hello", turn_id="1")
    assert calls[1]["messages"][2] == message
    assert calls[1]["messages"][3]["role"] == "tool"
    assert calls[0]["service_tier"] == "fast"
    assert calls[0]["reasoning"] == {"effort": "max"}
    assert "OPAQUE_PRIVATE" not in json.dumps(events.read_events(ctx.ws))


def test_interrupted_tool_not_reexecuted_and_result_exchange_repaired(ctx, monkeypatch):
    executed = []
    def interrupt(*_):
        executed.append(True)
        raise KeyboardInterrupt
    fake_calls(monkeypatch, [anthropic([{ "type": "tool_use", "id": "t1", "name": "file_read", "input": {}}], "tool_use")])
    with pytest.raises(KeyboardInterrupt):
        loop.run(ctx, registry(interrupt), cfg(), "System", "First", turn_id="1")
    saved = read_json(loop.history_path(ctx, "anthropic", "test-model"))
    assert saved["pending"]["executing"] == "t1"
    calls = []
    fake_calls(monkeypatch, [anthropic()], calls)
    loop.run(ctx, registry(interrupt), cfg(), "System", "New direction", turn_id="2")
    assert executed == [True]
    result = calls[0]["messages"][2]["content"][0]
    assert result["tool_use_id"] == "t1" and result["is_error"]
    assert "may have taken effect" in result["content"][0]["text"]
    assert calls[0]["messages"][-1]["content"] == "New direction"


@pytest.mark.parametrize(("provider", "response"), [
    ("anthropic", anthropic(reason="max_tokens")),
    ("anthropic", anthropic(reason="refusal")),
    ("anthropic", anthropic(reason=None)),
    ("openrouter", openrouter(reason="length")),
    ("openrouter", openrouter(reason="content_filter")),
    ("openrouter", openrouter({"content": "No", "refusal": "declined"})),
    ("openrouter", {"error": {"message": "private provider text"}}),
    ("openrouter", openrouter({"content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "file_read", "arguments": "not-json"}}]}, "tool_calls")),
])
def test_malformed_refused_or_incomplete_response_never_certified(ctx, monkeypatch, provider, response):
    executed = []
    fake_calls(monkeypatch, [response])
    with pytest.raises(loop.ProviderError):
        loop.run(ctx, registry(lambda *_: executed.append(True)), cfg(provider), "System", "Hello", turn_id="1")
    assert not executed
    assert len(read_json(loop.history_path(ctx, provider, "test-model"))["messages"]) == 1


def test_budget_exhaustion_saved_tool_result_but_no_final(ctx, monkeypatch):
    fake_calls(monkeypatch, [anthropic([{"type": "tool_use", "id": "t1", "name": "file_read", "input": {}}], "tool_use")])
    with pytest.raises(loop.ProviderError, match="step budget"):
        loop.run(ctx, registry(), cfg(max_turns=1), "System", "Hello", turn_id="1")
    state = read_json(loop.history_path(ctx, "anthropic", "test-model"))
    assert len(state["messages"]) == 3 and "pending" not in state
    assert "finished_turn" not in state


def test_absent_provider_usage_is_unavailable(ctx, monkeypatch):
    fake_calls(monkeypatch, [anthropic(usage={})])
    loop.run(ctx, registry(), cfg(), "System", "Hello", turn_id="1")
    assert read_json(ctx.ws.memory / "provider_context.json")["used_tokens"] is None
    assert loop._usage("openrouter", {"usage": {"completion_tokens": 10}}) == {}


def test_partial_stream_has_failed_snapshot_but_no_saved_assistant(ctx, monkeypatch):
    def post(provider, body, activity):
        activity("message", "Partial answer")
        raise loop.ProviderError("Stream ended before completion")
    monkeypatch.setattr(loop, "_post", post)
    with pytest.raises(loop.ProviderError):
        loop.run(ctx, registry(), cfg(), "System", "Hello", turn_id="1")
    assert len(read_json(loop.history_path(ctx, "anthropic", "test-model"))["messages"]) == 1
    messages = [r["data"] for r in events.read_events(ctx.ws) if r["kind"] == "message"]
    assert messages[-1]["text"] == "Partial answer" and messages[-1]["status"] == "failed"


def test_context_preflight_prevents_oversized_network_call_and_preserves_user(ctx, monkeypatch):
    monkeypatch.setattr(loop, "_post", lambda *_: pytest.fail("Network should not be reached"))
    with pytest.raises(loop.ProviderError, match="No model call was sent"):
        loop.run(ctx, registry(), cfg(context_window=500), "System", "Huge" * 100, turn_id="1")
    assert read_json(loop.history_path(ctx, "anthropic", "test-model"))["messages"][0]["content"] == "Huge" * 100


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
@pytest.mark.parametrize("budget", [300000, 1000000])
def test_multi_frame_preview_counts_pixels_not_image_transport(ctx, monkeypatch, provider, budget):
    # A realistic fitted noisy frame is about 580 KB, or 770 KB after base64 encoding.
    image = Image.frombytes("RGB", (1024, 1024), random.Random(1).randbytes(1024 * 1024 * 3))
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=70)
    frame = encoded.getvalue()
    assert 500000 < len(frame) < 600000
    tool = {"type": "tool_use", "id": "t1", "name": "file_read", "input": {}}
    or_tool = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "file_read", "arguments": "{}"}},
    ]}
    responses = ([anthropic([tool], "tool_use"), anthropic()] if provider == "anthropic"
                 else [openrouter(or_tool, "tool_calls"), openrouter()])
    calls = []
    fake_calls(monkeypatch, responses, calls)
    reg = registry(lambda *_: ToolResult(text="Two preview frames", images=[frame, frame]))
    assert loop.run(ctx, reg, cfg(provider, context_window=budget), "System", "Preview", turn_id="1") == "Done."
    assert len(json.dumps(calls[1])) > 1000000
    assert loop.estimate_tokens(calls[1], provider=provider) < 30000


def test_image_budget_depends_on_dimensions_and_text_still_enforces_context(ctx, monkeypatch):
    import base64
    image = Image.new("RGB", (1024, 1024), "white")
    png, jpeg = io.BytesIO(), io.BytesIO()
    image.save(png, format="PNG")
    image.save(jpeg, format="JPEG")
    def message(data):
        return {"type": "image", "source": {"type": "base64", "data": base64.b64encode(data).decode()}}
    assert loop.estimate_tokens(message(png.getvalue()), provider="anthropic") == loop.estimate_tokens(
        message(jpeg.getvalue()), provider="anthropic")
    assert loop._image_tokens((1024, 1024), "anthropic") == 37 * 37 + 512
    monkeypatch.setattr(loop, "_post", lambda *_: pytest.fail("Oversized text must not reach the network"))
    with pytest.raises(loop.ProviderError, match="No model call was sent"):
        loop.run(ctx, registry(), cfg(), "System", "Long text " * 40000, turn_id="1")


def test_role_allowlist_remains_authoritative_for_provider_calls(ctx, monkeypatch):
    ctx.role = "critic"
    executed = []
    reg = Registry([Tool("plan_update", "Write plan", {}, lambda *_: executed.append(True))])
    calls = []
    fake_calls(monkeypatch, [anthropic([{"type": "tool_use", "id": "t1", "name": "plan_update", "input": {}}], "tool_use"),
                             anthropic()], calls)
    loop.run(ctx, reg, cfg(), "System", "Hello", turn_id="1")
    assert not executed and "tools" not in calls[0]
    assert "may not call" in calls[1]["messages"][2]["content"][0]["content"][0]["text"]


def feed(stream, events_to_feed):
    for event in events_to_feed:
        stream.feed(json.dumps(event))
    return stream


def test_anthropic_stream_replays_signed_blocks_and_public_snapshots():
    activity = []
    stream = feed(loop._Stream("anthropic", lambda *args: activity.append(args)), [
        {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 100}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Checking"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "OPAQUE_PRIVATE"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Done."}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 10}},
        {"type": "message_stop"},
    ])
    data = stream.finish()
    assert data["content"][0]["signature"] == "OPAQUE_PRIVATE"
    assert data["usage"] == {"input_tokens": 100, "output_tokens": 10}
    assert ("thinking", "Checking") in activity and ("message", "Done.") in activity
    assert "OPAQUE_PRIVATE" not in json.dumps(activity)


def test_openrouter_stream_reassembles_arguments_and_reasoning_details():
    def chunk(delta, finish=None):
        return {"id": "m1", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    stream = feed(loop._Stream("openrouter"), [
        chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "file_read", "arguments": "{\"path\":"}}],
               "reasoning_details": [{"index": 0, "type": "reasoning.encrypted", "data": "ABC"}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": "\"x\"}"}}],
               "reasoning_details": [{"index": 0, "type": "reasoning.encrypted", "data": "DEF"}]}),
        chunk({}, "tool_calls"),
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
    ])
    stream.feed("[DONE]")
    message, calls, _, thoughts = loop._parse_openrouter(stream.finish())
    assert calls[0]["arguments"] == {"path": "x"}
    assert message["reasoning_details"][0]["data"] == "ABCDEF" and not thoughts


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_stream_without_terminal_event_is_error(provider):
    with pytest.raises(loop.ProviderError, match="before completion"):
        loop._Stream(provider).finish()


def test_proxy_tier_injection_only_responses_and_actual_tier_reporting():
    body = json.dumps({"model": "gpt-6-astra", "service_tier": "default"}).encode()
    assert json.loads(_tier_body("responses", body, "ultrafast"))["service_tier"] == "ultrafast"
    assert _tier_body("responses/123", body, "fast") == body
    assert _tier_body("images", body, "fast") == body
    tail = b'data: {"type":"response.completed","response":{"service_tier":"fast","usage":{"input_tokens":10}}}\n'
    assert _served_tier_from_stream(tail) == "fast"


def test_http_wrapper_uses_fixed_endpoint_fast_beta_and_redacts_metadata(monkeypatch):
    captured = []
    lines = [
        'data: {"type":"message_start","message":{"id":"secret-value","usage":{}}}', "",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":"Done"}}', "",
        'data: {"type":"content_block_stop","index":0}', "",
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}', "",
        'data: {"type":"message_stop"}', "",
    ]
    class Response:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def iter_lines(self):
            return iter(lines)
    class Client:
        def __init__(self, **_):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def stream(self, *args, **kwargs):
            captured.append((args, kwargs))
            return Response()
    monkeypatch.setattr(loop, "secrets", lambda: SimpleNamespace(require=lambda _: "secret-value"))
    monkeypatch.setattr(loop.httpx, "Client", Client)
    data = loop._post("anthropic", {"speed": "fast"})
    assert captured[0][0] == ("POST", "https://api.anthropic.com/v1/messages")
    assert captured[0][1]["headers"]["anthropic-beta"] == "fast-mode-2026-02-01"
    assert data["id"] == "[redacted]"


def test_http_errors_do_not_expose_upstream_text_or_key(monkeypatch):
    class Client:
        def __init__(self, **_):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def stream(self, *_args, **_kwargs):
            raise httpx.ConnectError("secret-value in provider internals")
    monkeypatch.setattr(loop, "secrets", lambda: SimpleNamespace(require=lambda _: "secret-value"))
    monkeypatch.setattr(loop.httpx, "Client", Client)
    with pytest.raises(loop.ProviderError) as error:
        loop._post("anthropic", {})
    assert "secret-value" not in str(error.value)


def test_job_lead_receives_explicit_config_override(ctx, monkeypatch):
    selected = cfg()
    seen = []
    monkeypatch.setattr(job_lead, "build_registry", lambda: registry())
    monkeypatch.setattr(job_lead, "Swarm", lambda *_: SimpleNamespace(shutdown=lambda: None))
    def lead(*args):
        seen.append(args[5])
        return "Done."
    monkeypatch.setattr(job_lead, "_lead", lead)
    job_lead.run(ctx.ws, "Hello", False, cfg=selected)
    assert seen == [selected]


def test_api_model_switch_receives_full_chat_and_missed_turns(ctx, monkeypatch):
    req = ctx.ws.load()
    req.chat = [ChatTurn(role="human", text="First" * 1000), ChatTurn(role="job_lead", text="Reply" * 1000),
                ChatTurn(role="human", text="Latest")]
    ctx.ws.save(req)
    seen = []
    def provider(*args, **kwargs):
        seen.append((args, kwargs))
        return "Done"
    monkeypatch.setattr(loop, "run", provider)
    session = {}
    job_lead._run_host(ctx.ws, registry(), None, "Latest", False, cfg(), ctx.ledger, session, [])
    assert len(seen[0][1]["initial_messages"][0]["content"]) == 5000
    assert len(seen[0][1]["initial_messages"][1]["content"]) == 5000
    assert seen[0][1]["initial_messages"][-1]["content"] != "Latest"
    assert session["key"] == "anthropic_api:test-model"
    path = loop.history_path(ctx, "anthropic", "test-model")
    write_json(path, {"messages": []})
    session["threads"][session["key"]]["replies"] = 0
    job_lead._run_host(ctx.ws, registry(), None, "Latest", False, cfg(), ctx.ledger, session, [])
    assert "Reply" * 1000 in seen[-1][0][4]
