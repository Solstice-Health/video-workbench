"""Model selection, private provider overrides and durable queue settings."""
from __future__ import annotations

import json
import os

import httpx
import pytest

from vwb import config, models
from vwb.agents import events, job_lead
from vwb.util.jsonio import read_json, write_json
from vwb.web.queue import TurnQueue
from vwb.web.server import Workbench
from vwb.workspace import Workspace
from vwb.workspace.ledger import Ledger
from vwb.workspace.request import SUBDIRS, ChatTurn, Request


@pytest.fixture
def settings(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    env = tmp_path / ".env"
    env.write_text("\n".join(f"{key}=fixture-{name}-key" for name, key in config.PROVIDERS.items()))
    monkeypatch.setenv("VWB_ENV_FILE", str(env))
    for key in config.PROVIDERS.values():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "RUNS_DIR", runs)
    config.secrets.cache_clear()
    yield runs
    config.secrets.cache_clear()


def workspace(runs):
    ws = Workspace(runs / "r-model-test")
    for directory in SUBDIRS:
        (ws.root / directory).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="Test models"))
    return ws


def test_keys_override_disconnect_reset_and_permissions(settings):
    original = config.find_env_file().read_bytes()
    assert config.secrets().openai == "fixture-openai-key"
    result = config.update_provider("openai", {"action": "save", "api_key": "fixture-replacement-key"})
    assert config.secrets().openai == "fixture-replacement-key"
    assert "fixture-replacement-key" not in json.dumps(result)
    path = settings / ".keys" / "provider_keys.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not os.environ.get("OPENAI_API_KEY")
    config.update_provider("openai", {"action": "clear"})
    assert not config.secrets().openai
    status = next(p for p in config.provider_status() if p["id"] == "openai")
    assert status == {"id": "openai", "label": "OpenAI", "configured": False, "source": "Disabled", "can_reset": True}
    config.update_provider("openai", {"action": "reset"})
    assert config.secrets().openai == "fixture-openai-key"
    assert config.find_env_file().read_bytes() == original


def test_key_store_refuses_symlinks_and_bad_values(settings, tmp_path):
    settings.mkdir()
    (settings / ".keys").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="regular local folder"):
        config.update_provider("openai", {"action": "save", "api_key": "fixture-key"})
    for key in ["short", "has spaces", None]:
        with pytest.raises(ValueError, match="valid API key"):
            config.update_provider("openai", {"action": "save", "api_key": key})


def test_selection_caps_and_runtime(settings):
    cat = models.catalog(settings)
    assert len(cat["providers"]) == 4
    assert next(m for m in cat["models"] if m["id"] == "muse")["model"] == "meta/muse-wonder"
    for model in ["fable51", "muse"]:
        with pytest.raises(ValueError, match="Fast is unavailable"):
            models.validate({"model_id": model, "fast": True}, settings)
    with pytest.raises(ValueError, match="Ultra Fast is unavailable"):
        models.validate({"model_id": "sol6", "ultra_fast": True}, settings)
    with pytest.raises(ValueError, match="either Fast or Ultrafast"):
        models.validate({"fast": True, "ultra_fast": True}, settings)
    for value in [{"context_tokens": 1000001}, {"context_tokens": True}, {"effort": "ultra"}, {"fast": 1}, {"unknown": 1}]:
        with pytest.raises(ValueError):
            models.validate(value, settings)
    cfg = models.runtime({"model_id": "opus55", "context_tokens": 300000, "effort": "xhigh", "fast": True}, settings)
    assert cfg["host"] == "anthropic_api" and cfg["model"] == "claude-opus-5-5"
    assert cfg["context_window"] == 300000 and cfg["auto_compact_tokens"] == 255000
    assert cfg["service_tier"] == "fast" and cfg["speed"] == "fast" and cfg["fallbacks"] == []


def test_custom_resolution_exact_id_caps_and_reload(settings, monkeypatch):
    calls = []
    def response(req):
        calls.append(str(req.url))
        if req.url.path.endswith("endpoints"):
            return httpx.Response(200, json={"data": {"endpoints": [{"tag": "Vendor/standard"}]}})
        return httpx.Response(200, json={"data": [{"id": "example/small-model", "name": "Small model", "context_length": 128000,
            "supported_parameters": ["tools"], "top_provider": {"max_completion_tokens": 8192}}]})
    real_client = httpx.Client
    monkeypatch.setattr(models.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(response), **kw))
    descriptor = models.resolve("openrouter/example/small-model", settings)
    assert descriptor["capabilities"] == {"contexts": [128000], "efforts": [], "fast": False, "ultra_fast": False}
    selected = {"model_id": "custom_openrouter", "custom_model": "example/small-model", "context_tokens": 128000, "effort": None}
    models.preferences({"default_selection": selected}, settings)
    assert models.catalog(settings)["selected_model"]["model"] == "example/small-model"
    assert models.runtime(selected, settings)["max_output_tokens"] == 8192
    with pytest.raises(ValueError, match="context budget"):
        models.validate({**selected, "context_tokens": 300000}, settings)
    with pytest.raises(ValueError, match="exact model ID"):
        models.resolve("example/typo", settings)
    for value in ["https://example/model", "../secret", "openrouter/no-owner", "example/model/extra"]:
        with pytest.raises(ValueError):
            models.canonical_openrouter(value)
    assert all(url.startswith("https://openrouter.ai/api/v1/models") for url in calls)


def test_selection_persistence_legacy_and_queue_freeze(settings):
    ws = workspace(settings)
    assert models.explicit_runtime(ws, settings) is None
    chosen = models.set_selection(ws, {"model_id": "sol61", "context_tokens": 300000}, settings)
    cfg = models.explicit_runtime(ws, settings)
    assert cfg["selection"] == chosen
    queue = TurnQueue(settings)
    try:
        queue.stop(ws)
        job = queue.enqueue(ws, message="first", main_config=cfg)
        cfg["selection"]["effort"] = "low"
        models.set_selection(ws, {"model_id": "muse"}, settings)
        frozen = queue.snapshot(ws)["queue"][0]
        assert frozen["main_config"]["model"] == "gpt-6.1-sol"
        assert frozen["main_config"]["selection"]["effort"] == "max"
        assert job["main_config"]["selection"]["context_tokens"] == 300000
    finally:
        queue.close()


def test_answers_freeze_config_and_fail_before_signing(settings, monkeypatch):
    ws = workspace(settings)
    queue = TurnQueue(settings)
    queue.stop(ws)
    app = Workbench(settings, queue=queue)
    monkeypatch.setattr("vwb.workspace.ledger.control_key", lambda: b"test-key")
    ticket = {"id": "ask-model", "status": "open", "question": "Keep it?", "kind": "choose",
              "options": [{"id": "yes", "label": "Yes"}], "lane": "editor", "key": "model", "created": "2026-10-13"}
    path = ws.asks / "tickets" / "ask-model.json"
    write_json(path, ticket)
    try:
        models.set_selection(ws, {"model_id": "opus55"}, settings)
        config.update_provider("anthropic", {"action": "clear"})
        with pytest.raises(ValueError, match="API key"):
            app.answer(ws, {"ticket": "ask-model", "option": "yes"})
        assert read_json(path)["status"] == "open"
        assert not (ws.ledger / "ledger.jsonl").exists()
        config.update_provider("anthropic", {"action": "reset"})
        result = app.answer(ws, {"ticket": "ask-model", "option": "yes"})
        assert result["job"]["main_config"]["model"] == "claude-opus-5-5"
    finally:
        queue.close()


def test_direct_context_measurement_is_model_scoped(settings):
    ws = workspace(settings)
    key = "openrouter_api:meta/muse-wonder"
    events.set_active_host(ws, key)
    write_json(ws.memory / "provider_context.json", {"key": key, "used_tokens": 1000, "limit_tokens": 300000,
        "estimated_tokens": 1400, "measured_at": "now", "breakdown": {"input_tokens": 900, "output_tokens": 100,
        "input_tokens_details": {"cached_tokens": 500}, "output_tokens_details": {"reasoning_tokens": 60}}})
    usage = events.context_usage(ws)
    assert usage["context"]["used_tokens"] == 1000 and usage["context"]["source"] == "provider_usage"
    assert usage["context"]["breakdown"]["cached_input_tokens"] == 500
    assert usage["context"]["estimated_tokens"] == 1400 and not usage["compact_available"]
    events.set_active_host(ws, "anthropic_api:claude-opus-5-5")
    assert events.context_usage(ws)["context"]["used_tokens"] is None


def test_explicit_gpt_switch_and_recovery_preserve_full_public_chat(settings, monkeypatch):
    from vwb.agents.host_codex import ThreadMissing
    ws = workspace(settings)
    req = ws.load()
    req.chat = [ChatTurn(role="human" if i % 2 == 0 else "job_lead", text=f"Marker {i}: " + "x" * 5000)
                for i in range(24)] + [ChatTurn(role="human", text="Latest")]
    ws.save(req)
    captured = []
    missing = [False]
    def run(_ws, _registry, _swarm, prompt, cfg, *, resume, session):
        captured.append(prompt)
        if missing[0]:
            missing[0] = False
            raise ThreadMissing("Missing test thread")
        session["thread_id"] = "new-thread"
        return "Done"
    monkeypatch.setattr(job_lead, "surface", lambda _: {"tools": {}, "docs": {}})
    monkeypatch.setattr(job_lead.host_codex, "run_lead", run)
    cfg = models.runtime(models.DEFAULT, settings)
    session, ledger = {}, Ledger(ws.ledger / "ledger.jsonl")
    job_lead._run_host(ws, None, None, "Latest", False, cfg, ledger, session, [])
    assert req.chat[0].text in captured[-1] and req.chat[-2].text in captured[-1]
    rec = session["threads"][session["key"]]
    rec["replies"] = 2
    job_lead._run_host(ws, None, None, "Latest", False, cfg, ledger, session, [])
    assert req.chat[4].text in captured[-1] and req.chat[23].text in captured[-1]
    assert req.chat[0].text not in captured[-1]  # already present on this model's saved thread
    missing[0] = True
    job_lead._run_host(ws, None, None, "Latest", False, cfg, ledger, session, [])
    assert req.chat[0].text in captured[-1]
    assert rec["config"] == cfg  # native compaction retains settings after missing-thread recovery
