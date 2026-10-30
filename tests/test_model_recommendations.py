"""Dated guidance reaches real tools without changing provider requests."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from vwb import prompts
from vwb.agents import swarm
from vwb.gateway import Gateway
from vwb.gateway import model_recommendations as guidance
from vwb.tools import build_registry, model_tools
from vwb.tools.base import Registry, ToolContext


@pytest.mark.parametrize("start,expected", [
    ("2026-10-30", "2027-02-28"),
    ("2026-10-31", "2027-02-28"),
    ("2023-10-30", "2024-02-29"),
    ("2023-10-31", "2024-02-29"),
    ("2023-12-31", "2024-04-30"),
    ("2024-02-29", "2024-06-29"),
    ("2026-11-30", "2027-03-30"),
    ("2026-01-31", "2026-05-31"),
])
def test_four_calendar_months_clamps_day_without_120_day_approximation(start, expected):
    assert guidance.add_calendar_months(date.fromisoformat(start), 4).isoformat() == expected


@pytest.mark.parametrize("updated,current,stale", [
    ("2026-10-30", "2026-10-30", False),
    ("2026-10-30", "2027-02-27", False),
    ("2026-10-30", "2027-02-28", False),
    ("2026-10-30", "2027-03-01", True),
    ("2023-10-31", "2024-02-28", False),
    ("2023-10-31", "2024-02-29", False),
    ("2023-10-31", "2024-03-01", True),
])
def test_refresh_starts_only_after_calendar_boundary(updated, current, stale):
    value = guidance.freshness(today=date.fromisoformat(current), last_updated=date.fromisoformat(updated))
    assert value["stale"] is stale
    assert value["requires_refresh"] is stale
    assert value["age_days"] == (date.fromisoformat(current) - date.fromisoformat(updated)).days
    if stale:
        assert "browse official provider documentation" in value["refresh_instruction"]
        assert "refresh model_catalog" in value["refresh_instruction"]


def test_future_review_date_cannot_claim_fresh_verification():
    result = guidance.freshness(today=date(2026, 10, 29))
    assert result["future_dated"] and result["requires_refresh"]
    assert result["age_days"] == -1


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        day = date(2026, 10, 30)

        @classmethod
        def now(cls, tz):
            assert tz is UTC
            return datetime.combine(cls.day, datetime.min.time(), tzinfo=UTC)

    monkeypatch.setattr(guidance, "datetime", Clock)
    return Clock


@pytest.fixture
def context(tmp_path, monkeypatch):
    ws = SimpleNamespace(root=tmp_path, work=tmp_path / "work", ledger=tmp_path / "ledger",
                         assets=tmp_path / "assets")
    for path in (ws.work, ws.ledger, ws.assets):
        path.mkdir()
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "allow"))
    return ToolContext(ws, "recommendations-test", "job_lead", Gateway(ws, actor="recommendations-test"))


@pytest.fixture
def access(monkeypatch):
    class Access:
        calls = []
        rows = [{"id": "minimax/hailuo-3", "supported_durations": [5, 6],
                 "supported_resolutions": ["2K"], "supports_seed": False}]

        def providers(self):
            self.calls.append(("providers", {}))
            return {"items": [{"id": "openrouter", "configured": True, "operations": [{"name": "video.create"}]}]}

        def catalog(self, **args):
            self.calls.append(("catalog", deepcopy(args)))
            return {"items": deepcopy(self.rows), "offset": args.get("offset", 0), "next_offset": None}

        def request(self, **args):
            self.calls.append(("request", deepcopy(args)))
            return {"provider": args["provider"], "operation": args["operation"],
                    "model": args["payload"].get("model"), "response": {"id": "job-test", "status": "pending"},
                    "artifacts": [], "response_path": "model-output/test/response.json"}

    obj = Access()
    monkeypatch.setattr(model_tools, "_access", lambda ctx: obj)
    return obj


def test_existing_registry_tools_expose_runtime_freshness_without_server_restart(context, access, clock):
    registry = build_registry()
    description = next(t.description for t in registry.for_role("job_lead") if t.name == "model_request")
    for expected in ("2026-10-30", "Gemini 4 Argon", "minimax/hailuo-3", "Kling 4", "Seedance",
                     "four calendar months", "browse official docs", "explicit model request", "sha256"):
        assert expected in description
    current = registry.call(context, "model_providers", {})
    assert not current.is_error, current.text
    assert current.data["items"][0]["operations"] == [{"name": "video.create"}]
    assert current.data["model_guidance"]["age_days"] == 0
    assert not current.data["model_guidance"]["stale"]
    clock.day = date(2027, 3, 1)
    aged = registry.call(context, "model_providers", {})
    assert aged.data["model_guidance"]["stale"]
    assert aged.data["model_guidance"]["as_of"] == "2027-03-01"
    assert aged.data["model_guidance"]["last_updated"] == "2026-10-30"
    assert {t["task"] for t in aged.data["model_guidance"]["tasks"]} >= {
        "video_generation", "video_understanding", "text_and_reasoning", "code", "image_generation",
        "image_understanding", "speech_generation", "audio_understanding_and_transcription", "embeddings",
    }


def test_catalog_keeps_live_rows_and_queries_separate_from_advisory_candidates(context, access, clock, monkeypatch):
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "block" if cap == "gen.video" else "allow"))
    args = {"provider": "openrouter", "modality": "video", "query": "hailuo", "limit": 30, "offset": 0}
    result = build_registry().call(context, "model_catalog", args)
    assert not result.is_error, result.text
    assert access.calls == [("catalog", args)]
    assert result.data["items"] == access.rows
    assert result.data["next_offset"] is None
    assert "model_guidance" in result.data
    candidates = [c for t in result.data["model_guidance"]["tasks"] for c in t["candidates"]]
    assert any(c["name"] == "Kling 4" and not c["verified_ids"] for c in candidates)
    # Neither a suggestion nor a missing live row is silently inserted/removed.
    assert all("recommendation" not in row for row in result.data["items"])


@pytest.mark.parametrize("model", ["google/gemini-3.1-pro-preview", "vendor/new-custom-model"])
def test_stale_guidance_preserves_explicit_native_payload_and_dispatches_once(context, access, clock, model):
    clock.day = date(2027, 6, 1)
    args = {"provider": "openrouter", "operation": "chat.completions", "purpose": "Explicit user model",
            "payload": {"model": model, "messages": [{"role": "user", "content": "Analyze the clip"}],
                        "provider": {"only": ["chosen-provider"]}, "temperature": 0.3,
                        "metadata": {"nested": ["preserved", {"value": 2}]}}}
    before = deepcopy(args)
    result = build_registry().call(context, "model_request", args)
    assert not result.is_error, result.text
    assert access.calls == [("request", before)]
    assert args == before
    assert result.data["model"] == model
    assert result.data["response"] == {"id": "job-test", "status": "pending"}
    assert result.data["model_guidance"]["requires_refresh"]
    assert "Preserve explicit user model requests" in result.data["model_guidance"]["refresh_instruction"]


def test_preferences_do_not_claim_unverified_routes_or_universal_winners():
    result = guidance.recommendations(today=date(2026, 10, 30))
    candidates = {c["name"]: c for t in result["tasks"] for c in t["candidates"]}
    assert candidates["Kling 4"]["id_evidence"] == "unverified_candidate"
    assert candidates["Kling 4"]["verified_ids"] == {}
    assert candidates["Gemini Omni 1.1"]["verified_ids"] == {"google": "gemini-omni-1.1-flash"}
    assert candidates["H3"]["verified_ids"] == {"openrouter": "minimax/hailuo-3"}
    assert candidates["Seedance 2.5"]["verified_ids"] == {"openrouter": "bytedance/seedance-2.5"}
    assert "not a universal ranking" in result["selection_policy"]
    for candidate in candidates.values():
        assert all(source in result["sources"] for source in candidate["sources"])
        if candidate["verified_ids"]:
            assert candidate["id_evidence"].startswith("official_")


def test_response_mutations_cannot_change_later_guidance():
    result = guidance.recommendations()
    result["tasks"][0]["candidates"][0]["verified_ids"]["google"] = "invented"
    result["sources"]["argon"] = "https://untrusted.invalid"
    original = guidance.recommendations()
    assert original["tasks"][0]["candidates"][0]["verified_ids"]["google"] == "gemini-4-argon"
    assert original["sources"]["argon"].startswith("https://ai.google.dev/")


def test_guidance_size_leaves_room_for_catalog_and_has_no_secret_lookup(monkeypatch):
    def fail():
        pytest.fail("Guidance must not read provider credentials")

    monkeypatch.setattr("vwb.config.secrets", fail)
    result = guidance.recommendations(today=date(2026, 10, 30))
    assert len(json.dumps(result).encode()) < 16000
    assert "api_key" not in json.dumps(result).lower()


def test_large_catalog_pagination_preserved_with_freshness_metadata(context, access, clock):
    access.rows = [{"id": f"vendor/model-{i}", "description": "x" * 1000} for i in range(100)]
    result = build_registry().call(context, "model_catalog", {"provider": "openrouter", "limit": 100, "offset": 10})
    assert not result.is_error, result.text
    assert result.data["page_size_limited"]
    assert result.data["next_offset"] == 10 + len(result.data["items"])
    assert result.data["items"] == access.rows[:len(result.data["items"])]
    assert len(result.text.encode()) < 60000


def test_model_selection_prompt_reaches_workers_and_main_profiles_require_discovery():
    # Main profiles use main_core.md rather than model_access.md. Their existing
    # discovery instruction reaches the new runtime tool guidance; workers also
    # receive the expanded shared prompt directly.
    for row in prompts.PROFILES:
        text = prompts.profile(row["id"])["content"]
        assert "model_providers" in text and "model_catalog" in text
        assert "current model IDs" in text
    for role in swarm.ROLE_SLOT:
        text = swarm.prompt(role)
        assert "2026-10-30" in text
        assert "four calendar months" in text
        assert "browse official provider documentation" in text
        assert "Do not default to models remembered from historical examples" in text
        assert "Preserve explicit user model choices" in text
        assert "sandbox_run(models=true)" in text
