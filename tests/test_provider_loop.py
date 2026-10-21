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
            "image_input_supported": True,
            "context_window": 300000, "native_context_window": 1000000, "max_output_tokens": 16384,
            "max_turns": 4, "service_tier": "default", "speed": "standard", **extra}


def test_post_refuses_anthropic_router_route_before_loading_any_key(monkeypatch):
    monkeypatch.setattr(loop, "secrets", lambda: pytest.fail("No key should be loaded for a forbidden route"))
    with pytest.raises(loop.ProviderError, match="direct Anthropic route"):
        loop._post("openrouter", {"model": "anthropic/claude-opus-5.5"})


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


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_text_only_model_omits_new_tool_pixels_before_persistence(ctx, monkeypatch, provider):
    encoded = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(encoded, "PNG")
    tool = {"type": "tool_use", "id": "t1", "name": "file_read", "input": {}}
    or_tool = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "file_read", "arguments": "{}"}}]}
    responses = ([anthropic([tool], "tool_use"), anthropic()] if provider == "anthropic"
                 else [openrouter(or_tool, "tool_calls"), openrouter()])
    calls = []
    fake_calls(monkeypatch, responses, calls)
    reg = registry(lambda *_: ToolResult(text="Source: source.pdf page 2", images=[encoded.getvalue()]))
    loop.run(ctx, reg, cfg(provider, image_input_supported=False), "System", "Inspect", turn_id="1")
    saved = loop.history_path(ctx, provider, "test-model").read_text()
    assert "base64" not in json.dumps(calls) and "base64" not in saved
    assert "no verified image-input support" in saved and "source.pdf page 2" in saved


def test_text_only_replay_omits_historical_pixels_but_preserves_native_assistant_data(ctx, monkeypatch):
    native_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,NOT_DECODED"}}
    signed = {"role": "assistant", "content": None, "reasoning_details": [
        {"type": "reasoning.encrypted", "data": "SIGNED_OPAQUE", "id": "r1"}], "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "file_read", "arguments": json.dumps(native_image)}}]}
    initial = [signed, {"role": "tool", "tool_call_id": "t1", "content": "Page reference: source.pdf"},
               {"role": "user", "content": [{"type": "text", "text": "Earlier tool image"}, native_image]}]
    before = copy.deepcopy(initial)
    calls = []
    fake_calls(monkeypatch, [openrouter()], calls)
    # Unknown custom config lacks the new flag, as old queued jobs can.
    settings = cfg("openrouter")
    settings.pop("image_input_supported")
    settings["selection"] = {"model_id": "custom_openrouter"}
    loop.run(ctx, registry(), settings, "System", "Continue", turn_id="2", initial_messages=initial)
    outgoing = calls[0]["messages"]
    assert outgoing[1] == signed
    assert outgoing[3]["content"][1]["type"] == "text"
    assert "no verified image-input support" in outgoing[3]["content"][1]["text"]
    assert initial == before
    saved = read_json(loop.history_path(ctx, "openrouter", "test-model"))
    assert saved["messages"][:3] == initial


def test_historical_anthropic_tool_result_filter_only_touches_content_blocks():
    image = {"type": "image", "source": {"type": "base64", "data": "UNCHANGED_IN_HISTORY"}}
    signed = {"role": "assistant", "content": [{"type": "thinking", "thinking": "Thought", "signature": "SIGNATURE"},
                                                {"type": "tool_use", "id": "t1", "input": image}]}
    user = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
                                         "content": [{"type": "text", "text": "a.pdf page 1"}, image]}]}
    original = copy.deepcopy([signed, user])
    wire = loop._without_images(original)
    assert wire[0] == signed and original == [signed, user]
    result = wire[1]["content"][0]
    assert result["tool_use_id"] == "t1" and not result["is_error"]
    assert result["content"][0]["text"] == "a.pdf page 1"
    assert result["content"][1]["type"] == "text"


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


@pytest.mark.parametrize("fragments,expected", [
    ([""], {}), ([], {}), (["", "{", "}"], {}),
    (["", '{"path":', '"notes.md"}'], {"path": "notes.md"}),
])
def test_anthropic_tool_stream_accepts_empty_deltas_and_complete_json(fragments, expected):
    stream = feed(loop._Stream("anthropic"), [
        {"type": "message_start", "message": {"id": "m1", "usage": {}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "t1", "name": "memory_read", "input": {}}},
        *[{"type": "content_block_delta", "index": 0,
           "delta": {"type": "input_json_delta", "partial_json": fragment}} for fragment in fragments],
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ])
    _, calls, _, _ = loop._parse_anthropic(stream.finish())
    assert calls[0]["arguments"] == expected


def test_anthropic_tool_stream_still_refuses_truncated_nonempty_json():
    with pytest.raises(loop.ProviderError, match="incomplete tool arguments"):
        feed(loop._Stream("anthropic"), [
            {"type": "message_start", "message": {"id": "m1", "usage": {}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "t1", "name": "file_write", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]).finish()


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
    assert captured[0][1]["headers"]["x-api-key"] == "secret-value"
    assert "authorization" not in captured[0][1]["headers"]
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
    assert len(seen) == 1
    assert {key: seen[0][key] for key in selected} == selected
    assert seen[0]["prompt"]["id"] == "descriptive"
    assert seen[0]["prompt"]["content"] and seen[0]["prompt"]["sha256"]


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


@pytest.mark.parametrize("saved", [
    {"provider": "openrouter", "host": "openrouter_api", "model": "anthropic/claude-opus-5.5"},
    {"provider": "openai", "host": "claude_sdk", "model": "claude-fable-5-1"},
])
def test_saved_anthropic_host_routes_direct_with_public_history(ctx, monkeypatch, saved):
    req = ctx.ws.load()
    req.chat = [ChatTurn(role="human", text="Earlier request"), ChatTurn(role="job_lead", text="Earlier reply"),
                ChatTurn(role="human", text="Continue")]
    ctx.ws.save(req)
    seen = []
    def provider(*args, **kwargs):
        seen.append((args[2], kwargs["initial_messages"]))
        return "Done"
    monkeypatch.setattr(loop, "run", provider)
    session = {}
    job_lead._run_host(ctx.ws, registry(), None, "Continue", False, cfg(**saved), ctx.ledger, session, [])
    routed, history = seen[0]
    assert routed["provider"] == "anthropic" and routed["host"] == "anthropic_api"
    assert not routed["model"].startswith("anthropic/")
    assert history[0]["content"] == "Earlier request" and history[1]["content"] == "Earlier reply"
    assert session["key"].startswith("anthropic_api:claude-")


def test_output_retry_preserves_history_and_never_dispatches_truncated_tools(ctx, monkeypatch):
    executed, calls, records = [], [], []
    selected = cfg(model="claude-opus-5-5", context_window=1000000)
    truncated = anthropic([
        {"type": "text", "text": "Incomplete draft"},
        {"type": "tool_use", "id": "unsafe", "name": "file_read", "input": {}},
    ], "max_tokens", usage={"input_tokens": 100, "output_tokens": 16384})
    complete = anthropic([{"type": "tool_use", "id": "safe", "name": "file_read", "input": {}}], "tool_use")
    fake_calls(monkeypatch, [truncated, complete, anthropic()], calls)
    monkeypatch.setattr(ctx.gateway, "record", lambda **row: records.append(row))
    reg = registry(lambda *_: (executed.append(True), ToolResult(text="Read once"))[1])
    assert loop.run(ctx, reg, selected, "System", "Continue", turn_id="retry") == "Done."
    assert [body["max_tokens"] for body in calls] == [16384, 128000, 128000]
    assert selected["max_output_tokens"] == 16384  # captured queue configuration is immutable
    assert calls[0]["messages"] == calls[1]["messages"]
    assert executed == [True] and [row["ok"] for row in records] == [False, True, True]
    saved = read_json(loop.history_path(ctx, "anthropic", selected["model"]))
    assert "unsafe" not in json.dumps(saved["messages"])
    receipt = read_json(ctx.ws.memory / saved["last_truncation"]["path"])
    assert receipt["response"] == truncated and receipt["max_tokens"] == 16384
    assert read_json(ctx.ws.memory / "provider_context.json")["max_output_tokens"] == 128000


def test_output_limit_at_verified_ceiling_stops_without_replaying_tools(ctx, monkeypatch):
    calls, executed, records = [], [], []
    selected = cfg(model="claude-opus-5-5", context_window=1000000, max_output_tokens=128000)
    fake_calls(monkeypatch, [anthropic([
        {"type": "tool_use", "id": "never", "name": "file_read", "input": {}},
    ], "max_tokens", usage={"input_tokens": 240000, "output_tokens": 128000})], calls)
    monkeypatch.setattr(ctx.gateway, "record", lambda **row: records.append(row))
    with pytest.raises(loop.ProviderError, match="128,000-token output allowance.*including thinking"):
        loop.run(ctx, registry(lambda *_: executed.append(True)), selected, "System", "Continue", turn_id="1")
    assert len(calls) == len(records) == 1 and not executed
    saved = read_json(loop.history_path(ctx, "anthropic", selected["model"]))
    assert saved["messages"] == [{"role": "user", "content": "Continue"}]
    assert (ctx.ws.memory / saved["last_truncation"]["path"]).is_file()
    context = read_json(ctx.ws.memory / "provider_context.json")
    assert context["used_tokens"] == 368000 and context["last_stop_reason"] == "max_tokens"
    assert context["auto_compaction_enabled"] and context["auto_compact_tokens"] == 850000


def test_uncertain_provider_failure_is_not_retried(ctx, monkeypatch):
    calls = []
    def fail(*_):
        calls.append(True)
        raise loop.ProviderError("Stream ended before completion")
    monkeypatch.setattr(loop, "_post", fail)
    with pytest.raises(loop.ProviderError, match="before completion"):
        loop.run(ctx, registry(), cfg(model="claude-opus-5-5"), "System", "Continue", turn_id="1")
    assert calls == [True]


def test_max_tokens_stream_retains_partial_json_only_for_private_recovery():
    stream = feed(loop._Stream("anthropic"), [
        {"type": "message_start", "message": {"id": "m1", "usage": {}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "t1", "name": "file_write", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
        {"type": "message_stop"},
    ])
    data = stream.finish()
    assert data["incomplete_tool_inputs"] == [{"index": 0, "partial_json": '{"path":'}]
    with pytest.raises(loop.ProviderError, match="max_tokens"):
        loop._parse_anthropic(data)


def test_native_compaction_stream_preserves_summary_without_public_leak():
    activity = []
    stream = feed(loop._Stream("anthropic", lambda *args: activity.append(args)), [
        {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 100}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "compaction", "content": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "compaction_delta", "content": "PRIVATE_SUMMARY: work/asset.png"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Done."}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 10}},
        {"type": "message_stop"},
    ])
    assistant, calls, answer, thought = loop._parse_anthropic(stream.finish())
    assert assistant["content"][0] == {"type": "compaction", "content": "PRIVATE_SUMMARY: work/asset.png"}
    assert not calls and answer == "Done." and not thought
    assert "PRIVATE_SUMMARY" not in json.dumps(activity)


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_stream_activity_is_rate_bounded_changed_only_and_flushes_final_text(monkeypatch, provider):
    now = [0.0]
    monkeypatch.setattr(loop.time, "monotonic", lambda: now[0])
    activity = []
    stream = loop._Stream(provider, lambda *args: activity.append(args))
    if provider == "anthropic":
        feed(stream, [{"type": "message_start", "message": {"id": "m1", "usage": {}}},
                      {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "thinking", "thinking": "", "signature": ""}}])
    for index in range(1000):
        now[0] = index * .01
        delta = ({"type": "content_block_delta", "index": 0,
                  "delta": {"type": "thinking_delta", "thinking": "thought "}}
                 if provider == "anthropic" else
                 {"choices": [{"index": 0, "delta": {"reasoning": "thought "}}]})
        stream.feed(json.dumps(delta))
    before_noise = len(activity)
    for _ in range(200):
        stream.feed(json.dumps({"type": "ping"} if provider == "anthropic" else {"choices": []}))
    assert len(activity) == before_noise  # unchanged snapshots never repeat
    if provider == "anthropic":
        feed(stream, [{"type": "content_block_stop", "index": 0},
                      {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
                      {"type": "message_stop"}])
    else:
        stream.feed("[DONE]")
    assert activity[-1] == ("thinking", "thought " * 1000)
    assert len(activity) <= 22  # around two snapshots/second, independent of chunk rate
    emitted_bytes = sum(len(value.encode()) for _, value in activity)
    unthrottled_bytes = sum(len("thought ") * n for n in range(1, 1001))
    assert emitted_bytes < unthrottled_bytes / 20


@pytest.mark.parametrize("summary", [None, "", "  ", "Retain work/asset.png and next steps"])
def test_compaction_prunes_only_outbound_view_with_valid_summary(ctx, monkeypatch, summary):
    calls = []
    selected = cfg(model="claude-opus-5-5", context_window=1000000, max_output_tokens=128000)
    native = {"type": "compaction", "content": summary}
    tool = {"type": "tool_use", "id": "t1", "name": "file_read", "input": {}}
    fake_calls(monkeypatch, [anthropic([native, tool], "tool_use"), anthropic()], calls)
    loop.run(ctx, registry(), selected, "System", "Original request", turn_id="1")
    saved = read_json(loop.history_path(ctx, "anthropic", selected["model"]))
    assert saved["messages"][0]["content"] == "Original request"
    assert saved["messages"][1]["content"][0] == native  # no added keys
    valid = isinstance(summary, str) and bool(summary.strip())
    assert (calls[1]["messages"][0]["role"] == "assistant") == valid
    assert saved.get("compaction_count", 0) == int(valid)
    assert "Retain work" not in json.dumps(events.read_events(ctx.ws))


def test_compaction_threshold_headers_counting_and_budget(monkeypatch):
    selected = cfg(model="claude-opus-5-5", context_window=1000000, max_output_tokens=128000)
    body = loop._request("anthropic", selected, "System", [{"role": "user", "content": "Hello"}], [])
    edit = body["context_management"]["edits"][0]
    assert edit["type"] == "compact_20260112" and edit["trigger"]["value"] == 850000
    assert "Do not call any tools" in edit["instructions"]
    monkeypatch.setattr(loop, "secrets", lambda: SimpleNamespace(require=lambda _: "secret-value"))
    headers = loop._anthropic_headers({**body, "speed": "fast"})
    assert headers["anthropic-beta"] == "fast-mode-2026-02-01,compact-2026-01-12"
    monkeypatch.setattr(loop, "estimate_tokens", lambda *_args, **_kwargs: 2000000)
    for actual in (240000, 900000):
        monkeypatch.setattr(loop, "_count_anthropic_tokens", lambda _, count=actual: count)
        assert loop._check_context(body, selected) == actual
    monkeypatch.setattr(loop, "_count_anthropic_tokens", lambda _: 1000001)
    with pytest.raises(loop.ProviderError, match="No model call was sent"):
        loop._check_context(body, selected)


def test_compaction_billing_sums_iterations_but_context_uses_final_message():
    usage = loop._usage("anthropic", {"usage": {
        "input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 40,
        "iterations": [
            {"type": "compaction", "input_tokens": 800000, "output_tokens": 2000, "cache_read_input_tokens": 1000},
            {"type": "message", "input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 40},
        ],
    }})
    assert usage["input_tokens"] == 140 and usage["total_tokens"] == 150
    assert usage["billed_input_tokens"] == 801140 and usage["billed_output_tokens"] == 2010
    assert usage["billed_cached_input_tokens"] == 1040


def test_prompt_change_resets_old_thinking_once_without_erasing_history(ctx, monkeypatch):
    old = {"type": "thinking", "thinking": "Prior reasoning", "signature": "OLD_SIGNED"}
    new = {"type": "thinking", "thinking": "New reasoning", "signature": "NEW_SIGNED"}
    calls = []
    fake_calls(monkeypatch, [anthropic([old, {"type": "text", "text": "First reply"}])], calls)
    loop.run(ctx, registry(), cfg(), "Original system", "First", turn_id="1")
    fake_calls(monkeypatch, [anthropic([new, {"type": "text", "text": "Second reply"}])], calls)
    loop.run(ctx, registry(), cfg(), "Selected new profile", "Second", turn_id="2")
    assert "OLD_SIGNED" not in json.dumps(calls[-1])
    assert "First reply" in json.dumps(calls[-1])
    fake_calls(monkeypatch, [anthropic()], calls)
    loop.run(ctx, registry(), cfg(), "Selected new profile", "Third", turn_id="3")
    assert "OLD_SIGNED" not in json.dumps(calls[-1]) and "NEW_SIGNED" in json.dumps(calls[-1])
    saved = read_json(loop.history_path(ctx, "anthropic", "test-model"))
    assert "OLD_SIGNED" in json.dumps(saved) and "NEW_SIGNED" in json.dumps(saved)
    assert len(saved["messages"]) == 6


def test_legacy_unknown_prefix_keeps_completed_tool_exchange_and_archived_reasoning(ctx, monkeypatch):
    signed = {"type": "thinking", "thinking": "Old reasoning", "signature": "OLD_SIGNED"}
    tool = {"type": "tool_use", "id": "done", "name": "file_read", "input": {}}
    path = loop.history_path(ctx, "anthropic", "test-model")
    messages = [{"role": "user", "content": "Original request"},
                {"role": "assistant", "content": [signed, tool]},
                *loop._tool_result("anthropic", {"id": "done", "name": "file_read"}, ToolResult(text="Completed already"))]
    write_json(path, {"provider": "anthropic", "model": "test-model", "messages": messages, "turn_id": "old"})
    calls = []
    fake_calls(monkeypatch, [anthropic()], calls)
    loop.run(ctx, registry(lambda *_: pytest.fail("Do not repeat completed tools")), cfg(), "New system", "Continue", turn_id="new")
    assert "OLD_SIGNED" not in json.dumps(calls[0]) and "Completed already" in json.dumps(calls[0])
    assert calls[0]["messages"][1]["content"] == [tool]
    assert read_json(path)["messages"][:3] == messages
