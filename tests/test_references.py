"""Reference discovery, queue persistence and delivery to the agent without paid calls."""
from __future__ import annotations

from pathlib import Path

import pytest

from vwb import orchestrator
from vwb.web.queue import TurnQueue
from vwb.workspace import Workspace, references
from vwb.workspace.request import SUBDIRS, ChatTurn, Request


@pytest.fixture
def ws(tmp_path):
    ws = Workspace(tmp_path / "r-refs")
    for sub in SUBDIRS:
        getattr(ws, sub).mkdir(parents=True)
    ws.save(Request(id=ws.id, message="A video edit"))
    (ws.work / "outputs" / "short 01").mkdir(parents=True)
    (ws.work / "planning").mkdir()
    return ws


def test_catalog_matches_agent_skills_and_safe_folders(ws, tmp_path):
    (ws.work / ".codex").mkdir()
    (ws.work / "planning" / "nested").mkdir()
    (ws.work / "external").symlink_to(tmp_path)
    result = references.catalog(ws)
    assert {s["id"] for s in result["skills"]} == {"clip", "remake", "shorts"}
    assert {f["id"] for f in result["folders"]} == {".", "outputs", "outputs/short 01", "planning", "planning/nested"}
    assert references.catalog()["folders"] == []


@pytest.mark.parametrize("path", ["../", "/etc", ".codex", "outputs/../planning", "missing", "planning\\nested", "\x00"])
def test_unavailable_folders_rejected(ws, path):
    with pytest.raises(ValueError):
        references.validate({"folders": [path]}, ws)


def test_symlink_folders_and_unknown_skills_rejected(ws, tmp_path):
    (ws.work / "alias").symlink_to(ws.work / "planning")
    for context in ({"folders": ["alias"]}, {"skills": ["does-not-exist"]}, {"folders": "planning"}, {"extra": []}):
        with pytest.raises(ValueError):
            references.validate(context, ws)
    with pytest.raises(ValueError):
        references.validate({"folders": ["planning"]})
    assert references.validate({"skills": ["remake", "remake"], "folders": ["."]}, ws) == {
        "skills": ["remake"], "folders": ["."]}


def test_queued_context_survives_restart(ws, tmp_path):
    context = {"skills": ["remake"], "folders": ["outputs/short 01"]}
    queue = TurnQueue(tmp_path)
    try:
        queue.stop(ws)
        job = queue.enqueue(ws, message="Improve this", context=context)
        assert job["context"] == context
    finally:
        queue.close()
    restarted = TurnQueue(tmp_path)
    try:
        assert restarted.snapshot(ws)["queue"][0]["context"] == context
        assert restarted.snapshot(ws)["paused"]
    finally:
        restarted.close()


def test_selected_context_reaches_agent_but_chat_text_stays_clean(ws, monkeypatch):
    monkeypatch.setattr(orchestrator.Workspace, "open", lambda request_id: ws)
    seen = []
    monkeypatch.setattr(orchestrator.job_lead, "run", lambda workspace, message, first_turn: seen.append(message) or "Done")
    monkeypatch.setattr(orchestrator, "ensure_review", lambda workspace: [])
    monkeypatch.setattr(orchestrator.page, "build", lambda workspace: Path("review.html"))
    context = {"skills": ["remake"], "folders": ["planning"]}
    orchestrator.chat("Improve the opening", request_id=ws.id, skip_index=True, context=context)
    assert "skill_read(name)" in seen[0] and '"remake"' in seen[0] and '"planning"' in seen[0]
    saved = ws.load().chat[-1]
    assert saved.text == "Improve the opening" and saved.context == context
    assert ChatTurn(role="human", text="Old history").context == {}


def test_missing_queued_folder_fails_before_new_chat_or_agent_call(ws, monkeypatch):
    monkeypatch.setattr(orchestrator.Workspace, "open", lambda request_id: ws)
    called = []
    monkeypatch.setattr(orchestrator.job_lead, "run", lambda *a, **k: called.append(True))
    with pytest.raises(ValueError):
        orchestrator.chat("Review", request_id=ws.id, context={"folders": ["deleted"]})
    assert not ws.load().chat and not called


def test_plugins_survive_queue_restart_and_reach_agent_as_gateway_bound_context(ws, tmp_path, monkeypatch):
    context = {"skills": [], "folders": [], "plugins": ["figma", "solstice"]}
    queue = TurnQueue(tmp_path)
    try:
        queue.stop(ws)
        assert queue.enqueue(ws, message="Use this design", context=context)["context"] == context
    finally:
        queue.close()
    queue = TurnQueue(tmp_path)
    try:
        assert queue.snapshot(ws)["queue"][0]["context"] == context
    finally:
        queue.close()
    monkeypatch.setattr(orchestrator.Workspace, "open", lambda request_id: ws)
    seen = []
    monkeypatch.setattr(orchestrator.job_lead, "run", lambda workspace, message, first_turn: seen.append(message) or "Ready")
    monkeypatch.setattr(orchestrator, "ensure_review", lambda workspace: [])
    monkeypatch.setattr(orchestrator.page, "build", lambda workspace: Path("review.html"))
    orchestrator.chat("Use this design", request_id=ws.id, skip_index=True, context=context)
    assert "integration_status" in seen[0] and "Solstice MCP gateway" in seen[0]
    assert ws.load().chat[-1].context == context and ws.load().chat[-1].text == "Use this design"


@pytest.mark.parametrize("plugins", [["not-installed"], "figma", ["figma", None], ["../credentials"]])
def test_unknown_or_malformed_plugins_are_rejected_without_starting_a_turn(ws, plugins):
    with pytest.raises(ValueError):
        references.validate({"plugins": plugins}, ws)
