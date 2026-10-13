"""Web gateway contract tests use a local httpx transport, never a paid provider."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from vwb.gateway import web
from vwb.gateway.core import Gateway
from vwb.util.hashing import sha256_text

KEY = "test-openrouter-secret-not-for-logs"
PROMPT = "Read the public example page and state its title."
TOOLS = [{"type": "openrouter:web_fetch", "parameters": {
    "engine": "exa", "max_uses": 1, "max_content_tokens": 2000,
}}]


@pytest.fixture
def harness(monkeypatch, tmp_path):
    cfg = {"model": "openai/gpt-5.4-mini", "max_output_tokens": 1024, "timeout_s": 45,
           "fallbacks": [{"model": "must-not-be-used"}]}
    monkeypatch.setattr(web, "slot", lambda name: dict(cfg) if name == "web.research" else None)
    monkeypatch.setattr(web, "secrets", lambda: SimpleNamespace(require=lambda name: KEY if name == "openrouter" else None))
    gateway = Gateway(SimpleNamespace(ledger=tmp_path), actor="test-web")
    requests, client_options = [], []
    real_client = httpx.Client

    def install(responder):
        def handle(request):
            requests.append(request)
            return responder(request)

        def client(**kwargs):
            client_options.append(kwargs)
            return real_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr(web.httpx, "Client", client)

    def rows():
        path = tmp_path / "tools.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return SimpleNamespace(gateway=gateway, cfg=cfg, requests=requests, client_options=client_options,
                           install=install, rows=rows)


def response(*, content="Example Domain: https://example.com", usage=None, **message):
    return {"id": "gen-public-test", "model": "openai/gpt-5.4-mini", "provider": "OpenAI",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content, **message}}],
            "usage": usage or {"prompt_tokens": 50, "completion_tokens": 20, "cost": 0.0004}}


def call(harness, **kwargs):
    return web.browse(harness.gateway, prompt=PROMPT, tools=deepcopy(TOOLS), max_tool_calls=1, **kwargs)


def test_single_bounded_request_preserves_evidence_and_accounting(harness):
    usage = {"prompt_tokens": 533, "completion_tokens": 62, "total_tokens": 595, "cost": 0.00067875,
             "prompt_tokens_details": {"cached_tokens": 12},
             "completion_tokens_details": {"reasoning_tokens": 4},
             "cost_details": {"upstream_inference_cost": 0.00067875},
             "server_tool_use_details": {"tool_calls_requested": 1, "tool_calls_executed": 1}}
    annotation = {"type": "url_citation", "url_citation": {
        "url": "https://example.com", "title": "Example", "content": "Public excerpt", "start_index": 0,
        "end_index": 14, "future_provider_field": True,
    }}
    data = response(usage=usage, annotations=[annotation, {"type": "provider_note", "note": "keep"}])
    harness.install(lambda _: httpx.Response(200, json=data))

    result = call(harness)

    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.url == web.ENDPOINT
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["tools"] == TOOLS and body["max_tool_calls"] == 1
    assert body["max_tokens"] == 1024 and body["stream"] is False
    assert body["model"] == "openai/gpt-5.4-mini" and "fallbacks" not in body
    assert harness.client_options[0]["follow_redirects"] is False
    assert harness.client_options[0]["trust_env"] is False
    assert harness.client_options[0]["timeout"].read == 45
    assert result["annotations"] == data["choices"][0]["message"]["annotations"]
    assert result["citations"] == [{key: value for key, value in annotation["url_citation"].items()
                                     if key != "future_provider_field"}]
    assert result["usage"] == usage and "input_tokens" not in result["usage"]
    assert result["retrieval"]["status"] == "sources_reported"
    assert result["retrieval"]["tool_calls_executed"] == 1
    assert "does not guarantee" in result["retrieval"]["note"]
    assert datetime.fromisoformat(result["retrieved_at"]).utcoffset().total_seconds() == 0
    assert result["response_id"] == "gen-public-test"
    record, = harness.rows()
    assert record["ok"] is True and record["provider"] == "openrouter"
    assert record["usage"]["input_tokens"] == 533
    assert record["usage"]["output_tokens"] == 62
    assert record["usage"]["input_tokens_details"] == {"cached_tokens": 12}
    assert record["usage"]["output_tokens_details"] == {"reasoning_tokens": 4}
    assert record["usage"]["cost_details"] == usage["cost_details"]
    assert len(record["input_sha256"]) == len(record["output_sha256"]) == 64
    assert all(value not in json.dumps(record) for value in (KEY, PROMPT, "Public excerpt"))


@pytest.mark.parametrize(("usage", "status"), [
    ({"server_tool_use_details": {"tool_calls_requested": 1, "tool_calls_executed": 1}}, "tool_activity_reported"),
    ({"server_tool_use": {"web_search_requests": 2}}, "tool_activity_reported"),
    ({"server_tool_use_details": {"tool_calls_requested": 1, "tool_calls_executed": 0}}, "unverified"),
    ({}, "unverified"),
])
def test_prose_urls_do_not_become_citations_or_verified_fetches(harness, usage, status):
    harness.install(lambda _: httpx.Response(200, json=response(usage=usage)))
    result = call(harness)
    assert result["citations"] == [] and result["annotations"] == []
    assert result["retrieval"]["status"] == status
    assert "https://example.com" in result["answer"]


def test_content_blocks_and_flat_annotations(harness):
    annotation = {"type": "url_citation", "url": "https://example.com", "title": "Public example"}
    harness.install(lambda _: httpx.Response(200, json=response(content=[
        {"type": "text", "text": "Public answer", "annotations": [annotation]},
        {"type": "reasoning", "text": "never expose this reasoning"},
    ])))
    result = call(harness)
    assert result["answer"] == "Public answer"
    assert result["citations"] == [{"url": "https://example.com", "title": "Public example"}]


@pytest.mark.parametrize("status", [301, 401, 429, 503])
def test_http_errors_are_sanitized_logged_and_not_retried(harness, status):
    body = {"error": {"message": f"Leaked upstream request: {KEY} {PROMPT}"},
            "usage": {"prompt_tokens": 9, "cost": 0.001}}
    harness.install(lambda _: httpx.Response(status, json=body, headers={"location": "https://other.example"}))
    with pytest.raises(web.WebResearchError, match=f"HTTP {status}") as exc:
        call(harness)
    assert KEY not in str(exc.value) and PROMPT not in str(exc.value)
    assert len(harness.requests) == 1
    record, = harness.rows()
    assert record["ok"] is False and record["usage"]["cost"] == 0.001
    assert record["usage"]["input_tokens"] == 9
    assert KEY not in json.dumps(record) and PROMPT not in json.dumps(record)


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failure_is_sanitized_and_not_retried(harness, exception):
    def fail(request):
        raise exception(f"Request leaked {KEY} {PROMPT}", request=request)

    harness.install(fail)
    with pytest.raises(web.WebResearchError, match="not retried") as exc:
        call(harness)
    assert KEY not in str(exc.value) and PROMPT not in str(exc.value)
    assert len(harness.requests) == 1
    record, = harness.rows()
    assert record["ok"] is False and record["usage"] == {}


@pytest.mark.parametrize("payload", [
    {}, [], {"choices": []}, {"choices": [{"message": None}]},
    response(content=" "), response(content={"text": "malformed"}), response(refusal="sensitive refusal"),
    response(content=[{"type": "refusal", "refusal": "sensitive refusal"}]),
    response(tool_calls=[{"function": {"name": "untrusted_local_command"}}]),
    {**response(), "error": {"message": "sensitive provider message"}},
    {**response(), "choices": [{"finish_reason": "tool_calls", "message": {"content": "Partial answer"}}]},
    {**response(), "choices": [{"finish_reason": {"malformed": True}, "message": {"content": "Partial"}}]},
])
def test_malformed_refused_and_unfinished_responses_fail_explicitly(harness, payload):
    harness.install(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(web.WebResearchError) as exc:
        call(harness)
    assert "sensitive" not in str(exc.value)
    record, = harness.rows()
    assert record["ok"] is False
    if isinstance(payload, dict) and "usage" in payload:
        assert record["usage"]["cost"] == payload["usage"]["cost"]
    assert len(harness.requests) == 1


def test_non_json_error_and_key_echo_are_not_exposed(harness):
    harness.install(lambda _: httpx.Response(200, text=f"<html>{KEY}</html>"))
    with pytest.raises(web.WebResearchError, match="invalid web research JSON"):
        call(harness)
    assert KEY not in json.dumps(harness.rows())
    harness.install(lambda _: httpx.Response(200, json=response(content=f"A provider accidentally echoed {KEY}",
        usage={"prompt_tokens": 1, "metadata": KEY})))
    result = call(harness)
    assert KEY not in json.dumps(result) and KEY not in json.dumps(harness.rows())
    assert "[redacted]" in result["answer"]


@pytest.mark.parametrize(("tools", "budget"), [
    (TOOLS, 0), (TOOLS, 31), (TOOLS, True), ([], 1),
    ([{"type": "function", "parameters": {"engine": "exa", "max_uses": 1}}], 1),
    ([{"type": "openrouter:apply_patch", "parameters": {"engine": "exa", "max_uses": 1}}], 1),
    ([{"type": "openrouter:web_fetch", "parameters": {"engine": "auto", "max_uses": 1}}], 1),
    ([{"type": "openrouter:web_fetch", "parameters": {"engine": "exa"}}], 1),
    ([{"type": "openrouter:web_fetch", "parameters": {"engine": "exa", "max_uses": 2}}], 1),
    (TOOLS + TOOLS, 2),
])
def test_invalid_or_unbounded_tools_never_make_a_request(harness, tools, budget):
    harness.install(lambda _: pytest.fail("No request should be made"))
    with pytest.raises(ValueError):
        web.browse(harness.gateway, prompt=PROMPT, tools=tools, max_tool_calls=budget)
    assert not harness.requests and not harness.rows()


def test_missing_secret_does_not_create_client_or_retry(harness, monkeypatch):
    def missing(_):
        raise RuntimeError("untrusted secret loader error")

    monkeypatch.setattr(web, "secrets", lambda: SimpleNamespace(require=missing))
    harness.install(lambda _: pytest.fail("No request should be made"))
    with pytest.raises(web.WebResearchError, match="Missing OpenRouter key"):
        call(harness)
    assert not harness.client_options
    record, = harness.rows()
    assert not record["ok"] and record["output_sha256"] == sha256_text("")
