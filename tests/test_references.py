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
    assert {s["id"] for s in result["skills"]} == {"clip", "remake", "shorts", "brand-context"}
    assert result['workflows'][0]['id'] == 'brand-context'
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


@pytest.mark.parametrize("plugins", [["figma", "solstice"], ["figma_direct"], ["figma_mcp"]])
def test_plugins_survive_queue_restart_and_reach_agent_as_gateway_bound_context(ws, tmp_path, monkeypatch, plugins):
    context = {"skills": [], "folders": [], "plugins": plugins}
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
    assert "integration='figma_direct'" in seen[0]
    assert ws.load().chat[-1].context == context and ws.load().chat[-1].text == "Use this design"


@pytest.mark.parametrize("plugins", [["not-installed"], "figma", ["figma", None], ["../credentials"]])
def test_unknown_or_malformed_plugins_are_rejected_without_starting_a_turn(ws, plugins):
    with pytest.raises(ValueError):
        references.validate({"plugins": plugins}, ws)


def test_bulk_folder_and_file_references_are_lazy_and_durable(ws, tmp_path, monkeypatch):
    folder = ws.work / "Batch of 50"
    folder.mkdir()
    for i in range(50):
        (folder / f"shot {i:02d}.txt").write_text("not automatically loaded into the prompt")
    context = {"skills": [], "folders": ["Batch of 50"], "files": ["Batch of 50/shot 49.txt"]}
    assert references.validate(context, ws) == context
    queue = TurnQueue(tmp_path)
    try:
        queue.stop(ws)
        queue.enqueue(ws, message="Reformat every input", context=context)
    finally:
        queue.close()
    queue = TurnQueue(tmp_path)
    try:
        assert queue.snapshot(ws)["queue"][0]["context"] == context
    finally:
        queue.close()
    seen = []
    monkeypatch.setattr(orchestrator.Workspace, "open", lambda _: ws)
    monkeypatch.setattr(orchestrator.job_lead, "run", lambda _, message, **kw: seen.append(message) or "Ready")
    monkeypatch.setattr(orchestrator, "ensure_review", lambda _: [])
    monkeypatch.setattr(orchestrator.page, "build", lambda _: Path("review.html"))
    orchestrator.chat("Reformat every input", request_id=ws.id, context=context)
    assert ws.load().chat[-1].context == context
    assert ws.load().chat[-1].attachments == []
    assert "Batch of 50" in seen[0] and "shot 49.txt" in seen[0]
    assert "not automatically loaded into the prompt" not in seen[0]
    assert "shot 00.txt" not in seen[0]


def test_path_search_reaches_past_initial_page_and_supports_spaces(ws, monkeypatch):
    for i in range(150):
        (ws.work / f"item{i:03d}.txt").write_text("content")
    deep = ws.work / "z batch" / "selected"
    deep.mkdir(parents=True)
    (deep / "shot 70.png").write_bytes(b"not decoded")
    initial = references.catalog(ws)
    assert initial["paths_truncated"]
    assert len(initial["folders"]) + len(initial["files"]) <= references.MAX_RESULTS
    result = references.catalog(ws, "work/z batch/selected/shot 70")
    assert [row["id"] for row in result["files"]] == ["z batch/selected/shot 70.png"]
    # A direct folder prefix remains useful even with a very small scan budget.
    monkeypatch.setattr(references, "MAX_SCAN", 3)
    result = references.catalog(ws, "z batch/selected/")
    assert not result["paths_truncated"] and result["scanned"] == 1
    assert result["folders"][0]["id"] == "z batch/selected"
    assert result["files"][0]["id"] == "z batch/selected/shot 70.png"
    exact = references.catalog(ws, "work/item149.txt")
    assert exact["files"][0]["id"] == "item149.txt" and exact["scanned"] == 0


def test_file_reference_scope_type_and_total_limit(ws, tmp_path):
    (ws.work / "notes.txt").write_text("notes")
    (ws.work / "link.txt").symlink_to(ws.work / "notes.txt")
    (ws.work / "external").symlink_to(tmp_path, target_is_directory=True)
    for path in ["../private", "/etc/passwd", ".", "planning", "link.txt", "external/file", ".secret"]:
        with pytest.raises(ValueError):
            references.validate({"files": [path]}, ws)
    for i in range(101):
        (ws.work / f"{i}.txt").touch()
    refs = [f"{i}.txt" for i in range(100)]
    assert len(references.validate({"files": refs}, ws)["files"]) == 100
    with pytest.raises(ValueError, match="100 references"):
        references.validate({"files": [*refs, "100.txt"]}, ws)
    assert references.catalog(ws, str(tmp_path))["files"] == []
    assert not any(row["id"].startswith(("external", "link.txt")) for row in references.catalog(ws)["files"])


def test_missing_file_reference_fails_before_turn(ws, monkeypatch):
    monkeypatch.setattr(orchestrator.Workspace, "open", lambda _: ws)
    with pytest.raises(ValueError, match="missing"):
        orchestrator.chat("Review", request_id=ws.id, context={"files": ["missing.txt"]})
    assert not ws.load().chat
