"""Video intake and on-demand registration without model calls."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vwb import orchestrator
from vwb.tools import build_registry
from vwb.tools.base import Registry, ToolContext
from vwb.workspace import Workspace, attachments, references
from vwb.workspace.media import register_inputs, register_work_file
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path):
    ws = Workspace(tmp_path / "r-media")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True)
    ws.save(Request(id=ws.id, message="Edit videos"))
    return ws


def video(path, payload=b"one"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + payload)
    return path


def test_selected_video_snapshot_is_independent_and_digest_deduplicated(ws):
    path = video(ws.work / "downloads" / "My video.MP4")
    original = path.read_bytes()
    first = register_work_file(ws, "downloads/My video.MP4")
    duplicate = video(ws.work / "another.mp4")
    assert register_work_file(ws, "another.mp4") == first
    assert Path(first.path).stat().st_ino != path.stat().st_ino
    assert Path(first.path).stat().st_mode & 0o222 == 0
    path.write_bytes(b"\x00\x00\x00\x18ftypisomchanged")
    second = register_work_file(ws, "downloads/My video.MP4")
    assert second.media_id == "src2" and second.sha256 != first.sha256
    assert Path(first.path).read_bytes() == original == duplicate.read_bytes()
    assert len(ws.load().sources) == 2


@pytest.mark.parametrize("skip_index", [False, True])
def test_all_attachments_and_selected_videos_reach_index_before_lead(ws, tmp_path, monkeypatch, skip_index):
    uploads = [video(tmp_path / f"upload{i}.mp4", str(i).encode()) for i in range(3)]
    video(ws.work / "download.mp4", b"download")
    video(ws.work / "duplicate.mp4", b"1")
    video(ws.work / "unselected.mp4", b"unselected")
    (ws.work / "notes.txt").write_text("not a source")
    events = []

    def index(workspace, **kw):
        assert len(workspace.load().sources) == 4
        events.append("index")

    def lead(workspace, message, **kw):
        assert events[-1] == "index"
        assert all(src.ref in message for src in workspace.load().sources)
        events.append("lead")
        return "done"

    monkeypatch.setattr(orchestrator, "build_index", index)
    monkeypatch.setattr(orchestrator.job_lead, "run", lead)
    monkeypatch.setattr(orchestrator, "ensure_review", lambda _: [])
    monkeypatch.setattr(orchestrator.page, "build", lambda _: Path("review.html"))
    context = {"files": ["download.mp4", "duplicate.mp4", "notes.txt"], "folders": ["."]}
    orchestrator.chat("Use these", attach=uploads, request_id=str(ws.root), context=context, skip_index=skip_index)
    assert events == ["index", "lead"]
    turn = ws.load().chat[-1]
    assert turn.text == "Use these" and len(turn.attachments) == 3
    # Replaying the same attachment refs and selections does not add sources.
    orchestrator.chat("Again", attachment_refs=turn.attachments, request_id=str(ws.root), context=context)
    assert len(ws.load().sources) == 4


def test_work_mapping_and_folder_selection_remains_lazy(ws, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "work").mkdir(parents=True)
    monkeypatch.setattr(Workspace, "project_root", property(lambda _: project))
    video(ws.work / "clip.mp4")
    video(ws.root / "work" / "clip.mp4", b"wrong request-local path")
    assert register_inputs(ws, [], {"folders": ["."]}) == []
    src, = register_inputs(ws, [], {"files": ["clip.mp4"]})
    assert src.original_path == str(project / "work" / "clip.mp4")
    assert Path(src.path).read_bytes() == (ws.work / "clip.mp4").read_bytes()


def test_lead_gets_current_project_note_after_moving_between_turns(ws, tmp_path, monkeypatch):
    from vwb.workspace.projects import ProjectStore

    store = ProjectStore(tmp_path)
    projects = [store.create("First project"), store.create("Second project")]
    messages = []
    monkeypatch.setattr(orchestrator.job_lead, "run", lambda _, message, **kw: messages.append(message) or "done")
    monkeypatch.setattr(orchestrator, "ensure_review", lambda _: [])
    monkeypatch.setattr(orchestrator.page, "build", lambda _: Path("review.html"))
    for row in projects:
        store.move(ws, row["id"])
        orchestrator.chat("Continue", request_id=str(ws.root), skip_index=True)
    for row, message in zip(projects, messages, strict=True):
        assert f"Current project: {row['name']} ({row['id']})" in message
        assert str(store.folder(row["id"]) / "work") in message
        assert "scoped to this conversation" in message and "media_register" in message
    assert "First project" not in messages[1]
    assert [turn.text for turn in ws.load().chat] == ["Continue", "Continue"]


@pytest.mark.parametrize("path", ["../outside.mp4", "/tmp/outside.mp4", "hidden/../clip.mp4", ".hidden/clip.mp4", "x\\clip.mp4", ".", "", "clip.mp4\x00"])
def test_registration_retains_reference_path_restrictions(ws, path):
    with pytest.raises((ValueError, OSError)):
        register_work_file(ws, path)
    assert not ws.load().sources


def test_symlinks_fifo_and_swap_after_selection_are_rejected(ws, tmp_path):
    outside = video(tmp_path / "outside" / "clip.mp4")
    (ws.work / "alias").symlink_to(outside.parent, target_is_directory=True)
    (ws.work / "link.mp4").symlink_to(outside)
    os.mkfifo(ws.work / "pipe.mp4")
    path = video(ws.work / "selected.mp4")
    context = references.validate({"files": ["selected.mp4"]}, ws)
    path.unlink()
    path.symlink_to(outside)
    for rel in ["alias/clip.mp4", "link.mp4", "pipe.mp4", context["files"][0]]:
        with pytest.raises((ValueError, OSError)):
            register_work_file(ws, rel)
    assert not ws.load().sources and not list(ws.media.iterdir())


def test_nonvideo_and_size_limit_leave_no_source(ws, monkeypatch):
    (ws.work / "fake.mp4").write_text("plain text")
    with pytest.raises(ValueError, match="not a supported video"):
        register_work_file(ws, "fake.mp4")
    video(ws.work / "big.mp4")
    monkeypatch.setattr(attachments, "MAX_BYTES", 8)
    with pytest.raises(ValueError, match="4 GB"):
        register_work_file(ws, "big.mp4")
    assert not ws.load().sources and not list(ws.media.iterdir())


def test_concurrent_registration_preserves_every_source_and_deduplicates(ws):
    for i in range(4):
        video(ws.work / f"{i}.mp4", str(i).encode())
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(lambda i: register_work_file(ws, f"{i % 4}.mp4"), range(12)))
    assert len(ws.load().sources) == len({ref.ref for ref in refs}) == 4


def test_media_register_registry_role_inheritance_policy_and_call_log(ws, monkeypatch):
    video(ws.work / "clip.mp4")
    registry = build_registry()
    ctx = ToolContext(ws=ws, agent="lead", role="job_lead", gateway=None)
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "block" if cap == "media.register" else "allow"))
    assert registry.call(ctx, "media_register", {"path": "clip.mp4"}).is_error
    assert not ws.load().sources
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "allow"))
    ctx.role = "code_explorer"
    assert registry.call(ctx, "media_register", {"path": "clip.mp4"}).is_error
    ctx.role = "worker"
    ctx.state["allowed_tools"] = ["file_read"]
    assert registry.call(ctx, "media_register", {"path": "clip.mp4"}).is_error
    ctx.state.clear()
    result = registry.call(ctx, "media_register", {"path": "clip.mp4"})
    assert not result.is_error and result.data["ref"] == ws.load().source().ref
    assert not (ws.index / "index.sqlite").exists()  # index.build remains a separate policy gate
    assert any(json.loads(line).get("tool") == "media_register" for line in (ws.ledger / "tools.jsonl").read_text().splitlines())
