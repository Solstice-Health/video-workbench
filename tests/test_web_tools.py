"""Web toolkit boundaries and exposure; no network or model calls."""

from types import SimpleNamespace

import pytest

from vwb import config
from vwb.gateway import Gateway
from vwb.tools import build_registry
from vwb.tools import web_tools as web
from vwb.tools.base import Registry, ToolContext


@pytest.fixture
def ctx(tmp_path):
    ws = SimpleNamespace(ledger=tmp_path / "ledger", work=tmp_path / "work")
    ws.ledger.mkdir()
    ws.work.mkdir()
    return ToolContext(ws=ws, agent="web-test", role="job_lead", gateway=Gateway(ws, actor="web-test"))


@pytest.fixture
def calls(monkeypatch):
    calls = []

    def browse(gateway, **kwargs):
        calls.append(kwargs)
        return {"answer": "A public source", "retrieval": {"status": "tool_activity_reported"}}

    monkeypatch.setattr(web.web, "browse", browse)
    return calls


def test_search_and_fetch_use_bounded_server_tools(ctx, calls):
    registry = build_registry()
    assert not registry.call(ctx, "web_search", {"query": "public docs", "domains": ["docs.python.org"]}).is_error
    search = calls[-1]
    assert search["tools"][0]["parameters"] == {"engine": "exa", "max_uses": 2,
        "allowed_domains": ["docs.python.org"], "max_results": 5, "max_total_results": 10,
        "search_context_size": "low"}
    assert not registry.call(ctx, "web_fetch", {"url": "https://docs.python.org/3/"}).is_error
    fetch = calls[-1]
    assert fetch["max_tool_calls"] == 1
    assert fetch["tools"][0]["type"] == "openrouter:web_fetch"
    assert fetch["tools"][0]["parameters"]["allowed_domains"] == ["docs.python.org"]
    assert "ledger" not in fetch["prompt"] and str(ctx.ws.work) not in fetch["prompt"]
    assert (ctx.ws.ledger / "tools.jsonl").is_file()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/", "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data", "https://example.com:8080/", "https://me:secret@example.com/",
    "http://[::1]/", "http://192.168.1.2/", "http://metadata.google.internal/", "http://127.1/",
    "https://example.com\\@127.0.0.1", "https://example.com/\nsecret"])
def test_nonpublic_or_credential_urls_never_reach_provider(ctx, calls, url):
    assert build_registry().call(ctx, "web_fetch", {"url": url}).is_error
    assert calls == []


@pytest.mark.parametrize("name,args", [
    ("web_search", {"query": "", "max_results": 5}),
    ("web_search", {"query": "x", "max_results": True}),
    ("web_search", {"query": "x", "max_results": 100}),
    ("web_search", {"query": "x", "domains": "example.com"}),
    ("web_search", {"query": "x", "domains": ["https://example.com"]}),
    ("web_research", {"task": "x", "max_steps": 9}),
    ("web_research", {"task": "x" * 8001}),
])
def test_invalid_bounds_fail_without_network(ctx, calls, name, args):
    assert build_registry().call(ctx, name, args).is_error
    assert calls == []


def test_research_limits_and_underlying_policy(ctx, calls, monkeypatch):
    registry = build_registry()
    assert not registry.call(ctx, "web_research", {"task": "Find Python docs", "max_steps": 4}).is_error
    assert calls[-1]["max_tool_calls"] == 4
    assert [t["type"] for t in calls[-1]["tools"]] == ["openrouter:web_search", "openrouter:web_fetch"]
    calls.clear()
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "block" if cap == "web.fetch" else "allow"))
    assert registry.call(ctx, "web_research", {"task": "x"}).is_error
    assert registry.call(ctx, "web_fetch", {"url": "https://example.com"}).is_error
    assert calls == []


def test_role_exposure(ctx, calls):
    registry = build_registry()
    for role in ("job_lead", "composer", "planner_b"):
        assert {"web_search", "web_fetch", "web_research"} <= {t.name for t in registry.for_role(role)}
    ctx.role = "scene_miner"
    assert registry.call(ctx, "web_search", {"query": "x"}).is_error
    assert calls == []


def test_openrouter_secret_loading_does_not_export_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sentinel-test-value\nUNRELATED_SECRET=never-load\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("VWB_ENV_FILE", str(env))
    config.secrets.cache_clear()
    try:
        assert config.secrets().require("openrouter") == "sentinel-test-value"
        import os
        assert "OPENROUTER_API_KEY" not in os.environ
        monkeypatch.setenv("OPENROUTER_API_KEY", "environment-wins")
        config.secrets.cache_clear()
        assert config.secrets().require("openrouter") == "environment-wins"
    finally:
        config.secrets.cache_clear()
