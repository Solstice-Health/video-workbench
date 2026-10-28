"""Real shared working files, independent private state, and lossless membership."""
from __future__ import annotations

import pytest
from filelock import FileLock

from vwb.canvas.compile import load_working
from vwb.orchestrator import _turn_lock
from vwb.util.jsonio import write_json
from vwb.web import workspace_files
from vwb.workspace import Workspace, references
from vwb.workspace.projects import ProjectConflict, ProjectStore
from vwb.workspace.request import SUBDIRS, ChatTurn, Request


def workspace(runs, rid):
    ws = Workspace(runs / rid)
    for name in SUBDIRS:
        (ws.root / name).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=rid, message="Hello", chat=[ChatTurn(role="human", text="Hello")]))
    return ws


def test_project_work_is_shared_across_tools_preview_and_canvas(tmp_path):
    a, b = workspace(tmp_path, "r-a"), workspace(tmp_path, "r-b")
    (a.work / "notes.txt").write_text("Shared brief")
    projects = ProjectStore(tmp_path)
    project = projects.create("Campaign")
    projects.move(a, project["id"])
    projects.move(b, project["id"])
    assert a.work == b.work
    assert not a.work.is_symlink()
    assert a.memory != b.memory
    assert a.load().project_id == project["id"]
    assert a.rel(b.work / "notes.txt") == "work/notes.txt"
    assert a.path("work/notes.txt") == b.work / "notes.txt"
    with workspace_files.open_file(b, "work/notes.txt") as stream:
        assert stream.read() == b"Shared brief"
    assert [r["name"] for r in workspace_files.listing(b, "work")["entries"]] == ["notes.txt"]
    assert workspace_files.search_files(b, "notes")["entries"][0]["path"] == "work/notes.txt"
    assert references.validate({"files": ["notes.txt"]}, b)["files"] == ["notes.txt"]
    write_json(a.work / "outputs/demo/canvas.json", {"duration_frames": 30, "time_map": [], "layers": []})
    assert load_working(b, "demo").output == "demo"
    b.add_chat("human", "Private to b")
    assert len(a.load().chat) == 1
    assert len(b.load().chat) == 2


def test_existing_project_conflict_never_overwrites_or_changes_membership(tmp_path):
    a, b = workspace(tmp_path, "r-a"), workspace(tmp_path, "r-b")
    (a.work / "a.txt").write_text("one")
    (b.work / "a.txt").write_text("two")
    (b.work / "new.txt").write_text("not partially published")
    projects = ProjectStore(tmp_path)
    project = projects.create("Campaign")
    projects.move(a, project["id"])
    with pytest.raises(ProjectConflict, match="conflict"):
        projects.move(b, project["id"])
    assert b.project_root is None
    assert (a.work / "a.txt").read_text() == "one"
    assert not (a.work / "new.txt").exists()
    assert (b.work / "a.txt").read_text() == "two"


def test_projects_do_not_follow_symlinks_or_bind_other_workbench(tmp_path):
    ws = workspace(tmp_path, "r-a")
    projects = ProjectStore(tmp_path)
    project = projects.create("Campaign")
    (ws.work / "leak").symlink_to(ws.memory, target_is_directory=True)
    with pytest.raises(ProjectConflict, match="symbolic link"):
        projects.move(ws, project["id"])
    write_json(ws.root / ".project.json", {"id": project["id"], "root": str(tmp_path.parent / ".projects" / project["id"])})
    with pytest.raises(ValueError):
        _ = ws.work


def test_project_moves_wait_for_turn_and_source_project_is_preserved(tmp_path):
    ws = workspace(tmp_path, "r-a")
    projects = ProjectStore(tmp_path)
    a, b = projects.create("A"), projects.create("B")
    projects.move(ws, a["id"])
    original = ws.work
    (original / "keep.txt").write_text("A")
    with FileLock(str(ws.project_root / "turn.lock")), pytest.raises(ProjectConflict):
        projects.move(ws, b["id"])
    projects.move(ws, b["id"])
    assert ws.work != original
    assert (original / "keep.txt").read_text() == "A"
    assert not (ws.work / "keep.txt").exists()


def test_edit_turns_serialize_across_project_without_blocking_other_projects(tmp_path):
    a, b, c = [workspace(tmp_path, f"r-{name}") for name in "abc"]
    projects = ProjectStore(tmp_path)
    project = projects.create("Together")
    projects.move(a, project["id"])
    projects.move(b, project["id"])
    first = _turn_lock(a)
    try:
        with pytest.raises(SystemExit):
            _turn_lock(b)
        other = _turn_lock(c)
        other.release()
    finally:
        first.release()
    again = _turn_lock(b)
    again.release()


def test_project_create_retries_and_legacy_message_ids_are_stable(tmp_path):
    projects = ProjectStore(tmp_path)
    first = projects.create("Campaign", operation_id="retry")
    assert projects.create("Campaign", operation_id="retry") == first
    with pytest.raises(ProjectConflict):
        projects.create("Other", operation_id="retry")
    ws = workspace(tmp_path, "r-a")
    raw = ws.load().model_dump(mode="json")
    raw["chat"][0].pop("id")
    write_json(ws.request_path, raw)
    initial = ws.load()
    assert ws.load().chat[0].id == initial.chat[0].id
    ws.add_chat("job_lead", "Done")
    assert ws.load().history_revision == initial.history_revision + 1
    assert ws.load().chat[0].id == initial.chat[0].id
