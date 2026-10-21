"""Bulk local inputs are independent snapshots available to every work-folder tool."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from vwb.tools.base import ToolContext
from vwb.tools.file_tools import file_list, file_read
from vwb.workspace import Workspace, local_imports
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "r-imports")
    for sub in SUBDIRS:
        (workspace.root / sub).mkdir(parents=True, exist_ok=True)
    workspace.save(Request(id=workspace.id, message="Reformat a batch"))
    return workspace


@pytest.fixture
def imports():
    service = local_imports.LocalImports()
    try:
        yield service
    finally:
        service.close()


def wait(service, ws, identity):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = service.get(ws, identity)
        if row["status"] in local_imports.TERMINAL:
            return row
        time.sleep(.01)
    raise AssertionError(f"Import {identity} did not finish")


def make_folder(tmp_path, count=25):
    folder = tmp_path / "Campaign assets"
    (folder / "Round 1").mkdir(parents=True)
    for index in range(count):
        (folder / "Round 1" / f"Asset {index:02}.txt").write_text(f"Content {index}")
    return folder


def test_bulk_folder_preserves_hierarchy_is_independent_and_visible_to_agent(ws, tmp_path, imports):
    folder = make_folder(tmp_path)
    job = imports.start(ws, str(folder))
    row = wait(imports, ws, job["id"])
    assert row["status"] == "ready", row
    assert row["files"] == row["total_files"] == 25
    assert row["bytes"] == row["total_bytes"] == sum(p.stat().st_size for p in folder.rglob("*.txt"))
    assert row["reference"]["kind"] == "folder"
    assert "source_path" not in row and "inventory" not in row and "entries" not in row
    root = ws.work / row["reference"]["id"]
    source = folder / "Round 1" / "Asset 00.txt"
    copy = root / "Round 1" / "Asset 00.txt"
    assert copy.read_bytes() == source.read_bytes()
    assert copy.stat().st_ino != source.stat().st_ino
    copy.write_text("Edited in sandbox")
    assert source.read_text() == "Content 0"
    source.write_text("Edited by user")
    assert copy.read_text() == "Edited in sandbox"
    ctx = ToolContext(ws, "test-agent", "job_lead", None)
    listed = file_list(ctx, {"glob": row["reference"]["id"] + "/**/*"}).data
    assert len(listed["files"]) == 25 and not listed["truncated"]
    assert "Edited in sandbox" in file_read(ctx, {"path": copy.relative_to(ws.work).as_posix()}).text
    inventory = json.loads((ws.root / ".local-imports" / row["id"] / "files.json").read_text())
    assert len(inventory["files"]) == 25
    assert inventory["files"][0]["path"].startswith("Round 1/")
    assert imports.list(ws)["jobs"][0]["id"] == row["id"]
    assert not (ws.root / ".local-imports" / row["id"] / "stage").exists()


def test_single_file_and_empty_file_are_references_not_inline_attachments(ws, tmp_path, imports):
    path = tmp_path / "Source footage.mov"
    path.write_bytes(b"")
    row = wait(imports, ws, imports.start(ws, str(path))["id"])
    assert row["status"] == "ready"
    assert row["total_files"] == 1 and row["bytes"] == 0
    assert row["reference"]["kind"] == "file"
    assert (ws.work / row["reference"]["id"]).read_bytes() == b""
    assert not ws.load().sources and not ws.load().chat
    assert not (ws.root / ".attachments").exists()


def test_child_links_hidden_and_dependencies_are_skipped_with_counts(ws, tmp_path, imports):
    folder = make_folder(tmp_path, count=1)
    external = tmp_path / "outside.txt"
    external.write_text("Never import via a link")
    (folder / "linked.txt").symlink_to(external)
    (folder / ".secret").write_text("Never read")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "dependency.txt").write_text("Skip tree")
    (folder / "empty folder").mkdir()
    row = wait(imports, ws, imports.start(ws, str(folder))["id"])
    assert row["status"] == "ready"
    assert row["files"] == 1 and row["skipped"] == 3
    assert row["skipped_details"] == {"hidden": 1, "dependency": 1, "symlink": 1, "unsupported": 0, "errors": 0}
    assert sorted(path.name for path in (ws.work / row["reference"]["id"]).rglob("*") if path.is_file()) == ["Asset 00.txt"]


@pytest.mark.parametrize("value", ["/", "~", str(Path.home()), "relative/path", "/missing/../file", "/tmp/\x00file"])
def test_invalid_broad_and_relative_paths_rejected_before_queue(ws, imports, value):
    with pytest.raises(ValueError):
        imports.start(ws, value)
    assert imports.list(ws) == {"jobs": []}


def test_root_symlink_hidden_and_missing_inputs_rejected(ws, tmp_path, imports):
    folder = make_folder(tmp_path, count=1)
    alias = tmp_path / "alias"
    alias.symlink_to(folder, target_is_directory=True)
    hidden = tmp_path / ".private.txt"
    hidden.write_text("hidden")
    for path in (alias, hidden, tmp_path / "missing"):
        with pytest.raises(ValueError):
            imports.start(ws, str(path))
    with pytest.raises(ValueError, match="parent"):
        imports.start(ws, str(tmp_path))
    assert imports.list(ws) == {"jobs": []}


def test_home_shorthand_uses_specific_child_without_granting_home(ws, tmp_path, imports, monkeypatch):
    folder = make_folder(tmp_path, count=1)
    monkeypatch.setenv("HOME", str(tmp_path))
    row = wait(imports, ws, imports.start(ws, f"~/{folder.name}")["id"])
    assert row["status"] == "ready"


@pytest.mark.parametrize("limit, value, expected", [("MAX_FILES", 2, "file import limit"),
                                                  ("MAX_ENTRIES", 2, "entry scan limit"),
                                                  ("MAX_BYTES", 2, "size limit"),
                                                  ("MAX_DEPTH", 1, "depth limit")])
def test_bounds_fail_visibly_without_partial_publication(ws, tmp_path, imports, monkeypatch, limit, value, expected):
    folder = make_folder(tmp_path, count=3)
    monkeypatch.setattr(local_imports, limit, value)
    row = wait(imports, ws, imports.start(ws, str(folder))["id"])
    assert row["status"] == "failed" and expected in row["error"]
    assert "reference" not in row
    assert not (ws.work / "imports" / row["id"]).exists()
    assert not (ws.root / ".local-imports" / row["id"] / "stage").exists()


def test_empty_folder_fails_with_helpful_error(ws, tmp_path, imports):
    folder = tmp_path / "Empty"
    folder.mkdir()
    (folder / ".hidden").touch()
    row = wait(imports, ws, imports.start(ws, str(folder))["id"])
    assert row["status"] == "failed" and "no importable" in row["error"]
    assert row["skipped"] == 1


def test_long_imported_path_fails_instead_of_creating_an_unselectable_file(ws, tmp_path, imports):
    folder = tmp_path / ("a" * 200)
    nested = folder / ("b" * 200)
    nested.mkdir(parents=True)
    (nested / ("c" * 100)).write_text("Not addressable by the workspace reference picker")
    row = wait(imports, ws, imports.start(ws, str(folder))["id"])
    assert row["status"] == "failed" and "512 bytes" in row["error"]
    assert not (ws.work / "imports" / row["id"]).exists()


def test_stream_copy_fallback_and_disk_space_check(ws, tmp_path, imports, monkeypatch):
    folder = make_folder(tmp_path, count=1)
    monkeypatch.setattr(local_imports, "_clone", lambda *args: False)
    row = wait(imports, ws, imports.start(ws, str(folder))["id"])
    assert row["status"] == "ready"
    assert (ws.work / row["reference"]["id"] / "Round 1/Asset 00.txt").read_text() == "Content 0"
    monkeypatch.setattr(local_imports, "DISK_RESERVE", 10**30)
    failed = wait(imports, ws, imports.start(ws, str(folder))["id"])
    assert failed["status"] == "failed" and "disk space" in failed["error"]
    assert not (ws.work / "imports" / failed["id"]).exists()


def test_source_changes_after_scan_fail_without_publishing(ws, tmp_path, imports, monkeypatch):
    folder = make_folder(tmp_path, count=2)
    original = imports._scan

    def changed(*args, **kwargs):
        inventory = original(*args, **kwargs)
        (folder / "Round 1/Asset 00.txt").write_text("Changed after scan")
        return inventory

    monkeypatch.setattr(imports, "_scan", changed)
    row = wait(imports, ws, imports.start(ws, str(folder))["id"])
    assert row["status"] == "failed" and "changed" in row["error"]
    assert not (ws.work / "imports" / row["id"]).exists()


def test_partial_copy_stays_private_then_cancel_cleans_it(ws, tmp_path, imports, monkeypatch):
    folder = make_folder(tmp_path, count=3)
    copying, release = threading.Event(), threading.Event()
    original = local_imports._copy_file
    calls = 0

    def slow(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            copying.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(local_imports, "_copy_file", slow)
    job = imports.start(ws, str(folder))
    try:
        assert copying.wait(5)
        assert not (ws.work / "imports" / job["id"]).exists()
        response = imports.cancel(ws, job["id"])
        assert response["cancel_requested"]
    finally:
        release.set()
    row = wait(imports, ws, job["id"])
    assert row["status"] == "cancelled"
    assert not (ws.work / "imports" / row["id"]).exists()
    assert not (ws.root / ".local-imports" / row["id"] / "stage").exists()


def test_symlink_publication_destination_does_not_write_outside_workspace(ws, tmp_path, imports):
    source = make_folder(tmp_path, count=1)
    outside = tmp_path / "Outside"
    outside.mkdir()
    (ws.work / "imports").symlink_to(outside, target_is_directory=True)
    row = wait(imports, ws, imports.start(ws, str(source))["id"])
    assert row["status"] == "failed" and "reference" not in row
    assert list(outside.iterdir()) == []


def test_existing_destination_is_not_replaced_even_if_empty(ws, tmp_path, imports, monkeypatch):
    source = make_folder(tmp_path, count=1)
    original = imports._publish

    def collide(workspace, job, identity, cancel, ready):
        (workspace.work / "imports" / identity).mkdir(parents=True)
        return original(workspace, job, identity, cancel, ready)

    monkeypatch.setattr(imports, "_publish", collide)
    row = wait(imports, ws, imports.start(ws, str(source))["id"])
    assert row["status"] == "failed" and "already exists" in row["error"]
    assert (ws.work / "imports" / row["id"]).is_dir()
    assert list((ws.work / "imports" / row["id"]).iterdir()) == []


def test_pinned_publication_directory_cannot_redirect_to_host_folder(ws, tmp_path, imports, monkeypatch):
    source = make_folder(tmp_path, count=1)
    outside = tmp_path / "Outside"
    outside.mkdir()
    original = local_imports._rename_new

    def swap(source_fd, source_name, destination_fd, target):
        (ws.work / "imports").rename(ws.work / "moved-imports")
        (ws.work / "imports").symlink_to(outside, target_is_directory=True)
        return original(source_fd, source_name, destination_fd, target)

    monkeypatch.setattr(local_imports, "_rename_new", swap)
    row = wait(imports, ws, imports.start(ws, str(source))["id"])
    assert row["status"] == "failed" and "folder changed" in row["error"]
    assert not list(outside.iterdir()) and not list((ws.work / "moved-imports").iterdir())


def test_per_workspace_concurrency_is_bounded_and_close_cancels(ws, tmp_path, imports, monkeypatch):
    source = make_folder(tmp_path, count=1)
    entered, release = threading.Event(), threading.Event()
    original = imports._scan

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(imports, "_scan", slow)
    one = imports.start(ws, str(source))
    two = imports.start(ws, str(source))
    try:
        assert entered.wait(5)
        with pytest.raises(ValueError, match="two imports"):
            imports.start(ws, str(source))
        imports.cancel(ws, one["id"])
        imports.cancel(ws, two["id"])
    finally:
        release.set()
    assert wait(imports, ws, one["id"])["status"] == "cancelled"
    assert wait(imports, ws, two["id"])["status"] == "cancelled"
    imports.close()
    with pytest.raises(ValueError, match="shutting down"):
        imports.start(ws, str(source))


def test_get_marks_abandoned_job_failed_without_replaying_it(ws, imports):
    identity = "a" * 32
    private = ws.root / ".local-imports" / identity
    (private / "stage").mkdir(parents=True)
    (private / "stage" / "partial.txt").write_text("private unfinished copy")
    (private / "manifest.json").write_text(json.dumps({
        "id": identity, "status": "running", "created": "2026-10-20", "name": "Abandoned",
        "files": 1, "total_files": 2, "bytes": 1, "total_bytes": 2, "skipped": 0,
    }))
    row = imports.get(ws, identity)
    assert row["status"] == "failed" and "server stopped" in row["error"]
    assert not (private / "stage").exists()
    assert not (ws.work / "imports").exists()
    assert imports.list(ws)["jobs"] == [row]


def test_raw_os_error_does_not_leak_host_paths_in_public_job(ws, tmp_path, imports, monkeypatch):
    source = make_folder(tmp_path, count=1)

    def failed(*args, **kwargs):
        raise PermissionError(13, "private host information", "/some/private/path")

    monkeypatch.setattr(local_imports, "_copy_file", failed)
    row = wait(imports, ws, imports.start(ws, str(source))["id"])
    assert row["status"] == "failed" and "/some/private" not in row["error"]
    assert row["skipped_details"]["errors"] == 1
    assert "reference" not in row
