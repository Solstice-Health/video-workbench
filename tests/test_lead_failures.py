"""Main-host failure reporting and fallback order, with no provider calls."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, sentinel

import pytest

from vwb.agents import job_lead
from vwb.agents.provider_loop import ProviderError
from vwb.workspace.ledger import Ledger


@pytest.fixture
def lead(tmp_path, monkeypatch):
    host = Mock()
    monkeypatch.setattr(job_lead, "_run_host", host)
    ledger = Ledger(tmp_path / "ledger.jsonl")

    def run(cfg):
        return job_lead._lead(sentinel.ws, sentinel.registry, sentinel.swarm,
                              "Continue editing", False, cfg, ledger, {}, [])

    return SimpleNamespace(run=run, host=host, ledger=ledger)


@pytest.mark.parametrize("provider,host,model", [
    ("anthropic", "anthropic_api", "claude-opus-5-5"),
    ("openrouter", "openrouter_api", "meta/muse-wonder"),
    ("openai", "codex", "gpt-6-astra"),
])
def test_single_selected_host_preserves_preflight_error(lead, provider, host, model):
    cfg = {"provider": provider, "host": host, "model": model, "fallbacks": []}
    upstream = ProviderError("The saved context exceeds the working budget. No model call was sent.")
    lead.host.side_effect = upstream

    with pytest.raises(RuntimeError) as error:
        lead.run(cfg)

    assert "only configured host" in str(error.value)
    assert f"{provider}/{model}" in str(error.value)
    assert str(upstream) in str(error.value)
    assert error.value.__cause__ is upstream
    assert "every main agent host failed" not in str(error.value)
    assert lead.host.call_count == 1
    assert lead.host.call_args.args[5] == cfg
    assert lead.host.call_args.kwargs == {"fallback": False}
    assert not lead.ledger.events(["flag.raised"])


def test_single_legacy_host_without_fallbacks_or_provider(lead):
    upstream = job_lead.HostUnavailable("Docker is not running")
    lead.host.side_effect = upstream

    with pytest.raises(RuntimeError) as error:
        lead.run({"host": "codex", "model": "gpt-6-astra"})

    assert "only configured host" in str(error.value)
    assert "codex/gpt-6-astra" in str(error.value)
    assert str(upstream) in str(error.value)
    assert error.value.__cause__ is upstream
    assert lead.host.call_count == 1
    assert not lead.ledger.events(["flag.raised"])


@pytest.fixture
def chain():
    return {"provider": "openai", "host": "codex", "model": "gpt-6-astra", "max_turns": 7,
            "fallbacks": [{"host": "tool_loop"},
                          {"provider": "anthropic", "host": "anthropic_api", "model": "claude-opus-5-5"}]}


@pytest.mark.parametrize("failures", [0, 1])
def test_success_stops_before_unused_fallbacks(lead, chain, failures):
    original = deepcopy(chain)
    upstream = job_lead.HostUnavailable("Docker is not running")
    lead.host.side_effect = [upstream] * failures + ["Finished editing"]

    assert lead.run(chain) == "Finished editing"

    calls = lead.host.call_args_list
    assert len(calls) == failures + 1
    assert calls[0].args[5] == original
    assert calls[0].kwargs == {"fallback": False}
    if failures:
        assert calls[1].args[5] == {**original, "host": "tool_loop"}
        assert calls[1].kwargs == {"fallback": True}
    raised = lead.ledger.events(["flag.raised"])
    assert len(raised) == failures
    if failures:
        assert raised[0]["data"]["kind"] == "SLOT-FALLBACK"
        assert raised[0]["data"]["evidence"] == {"error": str(upstream)}
    assert chain == original


@pytest.mark.parametrize("attempts", [2, 3])
def test_all_hosts_failed_reports_attempted_count_and_last_detail(lead, chain, attempts):
    chain["fallbacks"] = chain["fallbacks"][:attempts - 1]
    original = deepcopy(chain)
    errors = [job_lead.HostUnavailable("Docker is not running")]
    errors += [ProviderError(f"Configured host {i} rejected the request") for i in range(1, attempts)]
    lead.host.side_effect = errors

    with pytest.raises(RuntimeError) as error:
        lead.run(chain)

    assert f"all {attempts} attempted main agent hosts failed" in str(error.value)
    assert f"last error: {errors[-1]}" in str(error.value)
    assert error.value.__cause__ is errors[-1]
    calls = lead.host.call_args_list
    assert len(calls) == attempts
    assert [call.args[5] for call in calls] == [original] + [
        {**original, **fallback} for fallback in original["fallbacks"]]
    assert [call.kwargs["fallback"] for call in calls] == [False] + [True] * (attempts - 1)
    raised = lead.ledger.events(["flag.raised"])
    assert len(raised) == attempts - 1
    assert all(event["data"]["kind"] == "SLOT-FALLBACK" for event in raised)
    assert [event["data"]["evidence"]["error"] for event in raised] == [str(exc) for exc in errors[:-1]]
    assert chain == original
