"""Main-prompt selection is durable coaching, independent of model and authority."""
from __future__ import annotations

import shutil

import pytest

from vwb import prompts
from vwb.agents import job_lead
from vwb.config import slot
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import read_json, write_json
from vwb.web.queue import TurnQueue
from vwb.workspace import Workspace
from vwb.workspace.ledger import Ledger
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(TurnQueue, "_dispatch", lambda self: None)
    ws = Workspace(tmp_path / "r-prompts")
    for directory in SUBDIRS:
        (ws.root / directory).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="Make the edit"))
    return ws


def test_catalog_profiles_share_contract_and_minimal_stays_small(tmp_path):
    catalog = prompts.catalog(tmp_path)
    assert catalog["default_profile_id"] == "descriptive"
    assert {row["id"] for row in catalog["profiles"]} == {
        "descriptive", "minimal", "loose", "cursor_inspired", "claude_code_inspired", "codex_inspired"}
    core = (prompts.PROMPTS_DIR / "main_core.md").read_text().strip()
    for row in catalog["profiles"]:
        assert row["content"].startswith(core)
        assert row["sha256"] == sha256_text(row["content"])
        if row["kind"] == "inspired":
            assert row["sources"] and "adaptation" in row["content"]
    minimal = prompts.profile("minimal")["content"]
    assert len(minimal.split()) - len(core.split()) < 80
    assert len(minimal) < len(prompts.profile()["content"]) / 3


@pytest.mark.parametrize("value", [None, 3, {}, "../../etc/passwd", "current", "Minimal"])
def test_only_known_profile_ids_are_accepted(value):
    with pytest.raises(ValueError, match="known agent prompt"):
        prompts.validate(value)


def test_default_and_per_chat_preferences_are_independent(ws, tmp_path):
    prompts.preferences({"default_profile_id": "loose"}, tmp_path)
    assert prompts.selection(ws, tmp_path) == "loose"
    prompts.set_selection(ws, "minimal")
    prompts.preferences({"default_profile_id": "codex_inspired"}, tmp_path)
    assert prompts.selection(ws, tmp_path) == "minimal"
    assert prompts.selection(runs_dir=tmp_path) == "codex_inspired"
    with pytest.raises(ValueError):
        prompts.preferences({"default_profile_id": "minimal", "content": "override"}, tmp_path)


def test_queued_prompt_freezes_actual_text_and_legacy_slot_fallbacks(ws, tmp_path, monkeypatch):
    copied = tmp_path / "prompts"
    shutil.copytree(prompts.PROMPTS_DIR, copied)
    monkeypatch.setattr(prompts, "PROMPTS_DIR", copied)
    prompts.set_selection(ws, "minimal")
    queue = TurnQueue(tmp_path)
    try:
        queue.stop(ws)
        first = queue.enqueue(ws, message="First")
        expected = first["main_config"]["prompt"]
        assert expected["id"] == "minimal"
        assert first["main_config"]["fallbacks"] == slot("main")["fallbacks"]
        (copied / "main_minimal.md").write_text("A later edited prompt.")
        prompts.set_selection(ws, "loose")
        assert prompts.content(first["main_config"]) == expected["content"]
        second = queue.enqueue(ws, message="Second")
        assert second["main_config"]["prompt"]["id"] == "loose"
        assert read_json(ws.memory / "web_queue.json")["jobs"][0]["main_config"]["prompt"] == expected
    finally:
        queue.close()


def test_digest_corruption_is_not_silently_replaced():
    frozen = prompts.snapshot_profile("minimal")
    frozen["content"] += " changed"
    with pytest.raises(ValueError, match="digest"):
        prompts.content({"prompt": frozen})


def test_resume_can_change_only_prompt_without_changing_model_or_queued_work(ws, tmp_path):
    original = {"model": "old-model", "host": "anthropic_api", "effort": "max",
                "prompt": prompts.snapshot_profile("minimal")}
    source = {"id": "a" * 32, "status": "failed", "kind": "message", "message": "Original brief",
              "main_config": original}
    pending = {"id": "b" * 32, "status": "queued", "kind": "message", "message": "Later",
               "main_config": original}
    write_json(ws.memory / "web_queue.json", {"jobs": [source, pending], "paused": True})
    queue = TurnQueue(tmp_path)
    try:
        with queue.lock:
            result = queue.resume(ws, job_id=source["id"], prompt=prompts.snapshot_profile("loose"))
            continuation = result["queue"][0]
            assert continuation["main_config"]["model"] == "old-model"
            assert continuation["main_config"]["effort"] == "max"
            assert continuation["main_config"]["prompt"]["id"] == "loose"
            assert result["queue"][1]["main_config"]["prompt"]["id"] == "minimal"
            assert read_json(ws.memory / "web_queue.json")["jobs"][0]["main_config"] == original
            with pytest.raises(ValueError, match="interrupted turn"):
                queue.resume(ws, prompt=prompts.snapshot_profile("loose"))
    finally:
        queue.close()


def test_native_thread_keeps_history_and_receives_replacement_profile(ws, monkeypatch):
    calls = []
    def run(_ws, _registry, _swarm, text, cfg, *, resume, session):
        calls.append((text, cfg["prompt"], resume))
        session["thread_id"] = "native-thread"
        return "Done"
    monkeypatch.setattr(job_lead, "surface", lambda _: {"tools": {}, "docs": {}})
    monkeypatch.setattr(job_lead.host_codex, "run_lead", run)
    registry, swarm, session, ledger = None, None, {}, Ledger(ws.ledger / "ledger.jsonl")
    cfg = {"host": "codex", "provider": "openai", "model": "test-model", "prompt": prompts.snapshot_profile("minimal")}
    job_lead._run_host(ws, registry, swarm, "Start", True, cfg, ledger, session, [])
    cfg["prompt"] = prompts.snapshot_profile("loose")
    job_lead._run_host(ws, registry, swarm, "Continue", False, cfg, ledger, session, [])
    assert calls[0][2] is None and calls[1][2] == "native-thread"
    assert "Your instructions were updated" in calls[1][0]
    assert cfg["prompt"]["content"] in calls[1][0]
    assert "# Minimal" not in calls[1][0]
    job_lead._run_host(ws, registry, swarm, "Again", False, cfg, ledger, session, [])
    assert "Your instructions were updated" not in calls[2][0]


def test_direct_provider_uses_frozen_prompt_and_surface_hash(ws, monkeypatch):
    seen = []
    monkeypatch.setattr(job_lead, "surface", lambda _: {"tools": {}, "docs": {}})
    monkeypatch.setattr(job_lead.provider_loop, "run", lambda _ctx, _registry, _cfg, system, _text, **kwargs: seen.append(system) or "Done")
    cfg = {"host": "anthropic_api", "provider": "anthropic", "model": "test-model",
           "prompt": prompts.snapshot_profile("codex_inspired")}
    session = {}
    job_lead._run_host(ws, None, None, "Continue", False, cfg, Ledger(ws.ledger / "ledger.jsonl"), session, [])
    assert seen == [cfg["prompt"]["content"]]
    assert session["threads"][session["key"]]["surface"]["docs"]["instructions"] == cfg["prompt"]["sha256"][:12]


def test_legacy_jobs_do_not_follow_later_preferences(ws, tmp_path):
    prompts.preferences({"default_profile_id": "loose"}, tmp_path)
    prompts.set_selection(ws, "minimal")
    assert prompts.content({"model": "old-saved-config"}) == prompts.profile("descriptive")["content"]


def test_legacy_native_thread_without_fingerprint_receives_selected_instructions():
    cfg = {"prompt": prompts.snapshot_profile("minimal")}
    note = job_lead.surface_note(None, {"tools": {}, "docs": {}}, cfg=cfg)
    assert "Your instructions were updated" in note and cfg["prompt"]["content"] in note
