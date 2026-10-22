"""Default model modalities cannot bypass media policies on native chat routes."""
from types import SimpleNamespace

import pytest

from vwb.gateway import Gateway
from vwb.tools import build_registry, model_tools
from vwb.tools.base import Registry, ToolContext


@pytest.fixture
def ctx(tmp_path):
    ws = SimpleNamespace(root=tmp_path, work=tmp_path / "work", ledger=tmp_path / "ledger", assets=tmp_path / "assets")
    for path in (ws.work, ws.ledger, ws.assets):
        path.mkdir()
    return ToolContext(ws, "model-policy-test", "job_lead", Gateway(ws))


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def request(**args):
        seen.append(args)
        return {"provider": args["provider"], "operation": args["operation"], "model": "a-model",
                "response": {"text": "result"}, "artifacts": []}

    monkeypatch.setattr(model_tools, "_access", lambda _: SimpleNamespace(request=request))
    return seen


@pytest.mark.parametrize("provider,operation", [
    ("openai", "responses"), ("openai", "chat.completions"),
    ("openrouter", "responses"), ("openrouter", "chat.completions"),
    ("google", "generate_content"),
])
@pytest.mark.parametrize("capability", ["gen.image", "gen.video", "gen.audio"])
def test_default_model_output_respects_blocked_media_before_request(ctx, calls, monkeypatch, provider, operation, capability):
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "block" if cap == capability else "allow"))
    result = build_registry().call(ctx, "model_request", {
        "provider": provider, "operation": operation, "payload": {"model": "new-model-with-unknown-defaults"}})
    assert result.is_error and capability in result.text
    assert not calls


@pytest.mark.parametrize("payload", [
    {"modalities": ["text"]},
    {"generationConfig": {"responseModalities": ["TEXT"]}},
    {"generation_config": {"response_modalities": ["TEXT"]}},
])
def test_caller_text_only_field_does_not_grant_unknown_model_output(ctx, calls, monkeypatch, payload):
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "block" if cap == "gen.image" else "allow"))
    result = build_registry().call(ctx, "model_request", {
        "provider": "google", "operation": "generate_content", "payload": {"model": "new-model", **payload}})
    assert result.is_error and "gen.image" in result.text
    assert not calls


def test_ambiguous_request_parks_for_required_media_permission(ctx, calls, monkeypatch):
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "ask" if cap == "gen.audio" else "allow"))
    questions = []
    monkeypatch.setattr("vwb.ask.broker.Broker", lambda _: SimpleNamespace(
        ask=lambda **args: questions.append(args) or {"id": "permission-ticket", "status": "open"}))
    result = build_registry().call(ctx, "model_request", {
        "provider": "openai", "operation": "responses", "payload": {"model": "new-model"}})
    assert result.data["parked"] and result.data["ticket"] == "permission-ticket"
    assert questions[0]["key"] == "allow:gen.audio"
    assert not calls


def test_ambiguous_request_records_flag_policy_without_skipping_dispatch(ctx, calls, monkeypatch):
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "flag" if cap == "gen.audio" else "allow"))
    result = build_registry().call(ctx, "model_request", {
        "provider": "openrouter", "operation": "chat.completions", "payload": {"model": "new-model"}})
    assert not result.is_error and len(calls) == 1
    flagged = ctx.ledger.events(["flag.raised"])
    assert len(flagged) == 1 and "gen.audio" in str(flagged)


def test_fully_allowed_policy_keeps_general_text_requests_available(ctx, calls, monkeypatch):
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "allow"))
    assert model_tools._generation_capabilities({"operation": "chat.completions", "payload": {}}) == []
    result = build_registry().call(ctx, "model_request", {
        "provider": "openrouter", "operation": "chat.completions", "payload": {"model": "new-model"}})
    assert not result.is_error and len(calls) == 1
    assert not ctx.ledger.events(["flag.raised"])


def test_embedding_route_does_not_require_media_generation_permissions(ctx, calls, monkeypatch):
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "block" if cap.startswith("gen.") else "allow"))
    result = build_registry().call(ctx, "model_request", {
        "provider": "openai", "operation": "embeddings", "payload": {"model": "embedding-model"}})
    assert not result.is_error and len(calls) == 1
