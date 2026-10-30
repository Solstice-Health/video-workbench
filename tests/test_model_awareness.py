"""Provider aliases and capability guidance agree with the host credential boundary."""
from __future__ import annotations

import json

import pytest

from vwb import config, prompts
from vwb.agents import job_lead, swarm


@pytest.fixture
def key_settings(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("VWB_ENV_FILE", str(env_file))
    for key in config._SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)
    config.secrets.cache_clear()
    yield env_file
    config.secrets.cache_clear()


@pytest.mark.parametrize(
    ("file_text", "environment", "expected", "source"),
    [
        ("GEMINI_API_KEY=fixture-file-alias\n", {}, "fixture-file-alias", ".env"),
        ("GOOGLE_API_KEY=fixture-canonical\nGEMINI_API_KEY=fixture-alias\n", {}, "fixture-canonical", ".env"),
        ("GOOGLE_API_KEY=fixture-file\n", {"GEMINI_API_KEY": "fixture-env-alias"}, "fixture-env-alias", "environment"),
        ("GEMINI_API_KEY=fixture-file-alias\n", {"GOOGLE_API_KEY": "fixture-env"}, "fixture-env", "environment"),
        ("", {"GOOGLE_API_KEY": "fixture-env", "GEMINI_API_KEY": "fixture-env-alias"}, "fixture-env", "environment"),
        ("GOOGLE_API_KEY=\nGEMINI_API_KEY=fixture-alias\n", {}, "fixture-alias", ".env"),
    ],
)
def test_google_alias_precedence_matches_visible_provider_status(key_settings, monkeypatch, file_text, environment, expected, source):
    key_settings.write_text(file_text)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert config.secrets().google == expected
    status = next(row for row in config.provider_status() if row["id"] == "google")
    assert status == {"id": "google", "label": "Google", "configured": True, "source": source, "can_reset": False}
    assert expected not in json.dumps(status)


def test_google_alias_respects_managed_override_disable_and_reset(key_settings, monkeypatch):
    key_settings.write_text("GEMINI_API_KEY=fixture-alias\n")
    original = key_settings.read_bytes()
    status = config.update_provider("google", {"action": "save", "api_key": "fixture-managed"})
    assert config.secrets().google == "fixture-managed"
    assert next(row for row in status if row["id"] == "google")["source"] == "Workbench"
    config.update_provider("google", {"action": "clear"})
    assert config.secrets().google == ""
    status = next(row for row in config.provider_status() if row["id"] == "google")
    assert status["source"] == "Disabled" and status["can_reset"] and not status["configured"]
    config.update_provider("google", {"action": "reset"})
    assert config.secrets().google == "fixture-alias"
    assert key_settings.read_bytes() == original
    assert "GEMINI_API_KEY" not in config.os.environ


def test_main_profiles_and_roles_with_model_access_receive_guidance():
    texts = [prompts.profile(row["id"])["content"] for row in prompts.PROFILES]
    texts += [swarm.prompt(role) for role in swarm.ROLE_SLOT if role != 'brand_context']
    # Ingestion deliberately has no model_request, shell or network tool.
    assert 'model_request' not in swarm.prompt('brand_context')
    for text in texts:
        for name in ("model_providers", "model_catalog", "model_request", "sandbox_run(models=true)", "OpenRouter"):
            assert name in text
        assert "host" in text and ".env" in text


def test_native_resume_announces_new_model_tools_and_replaces_stale_instructions():
    cfg = {"prompt": prompts.snapshot_profile("minimal")}
    old = {"tools": {"file_read": "unchanged"}, "docs": {"instructions": "previous-prompt"}}
    new = {"tools": {"file_read": "unchanged", "model_providers": "new", "model_catalog": "new", "model_request": "new"},
           "docs": {"instructions": cfg["prompt"]["sha256"][:12]}}
    note = job_lead.surface_note(old, new, cfg=cfg)
    assert "new tools:" in note and "model_request" in note
    assert "Your instructions were updated" in note
    assert cfg["prompt"]["content"] in note
