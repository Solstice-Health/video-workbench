"""Project public files are shared; transfer staging and job state stay private."""
from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from vwb.browser import service as browser_service
from vwb.browser.service import BrowserSession
from vwb.studio import assets
from vwb.studio.models import JobInput
from vwb.studio.service import prepare, private_job
from vwb.workspace import Workspace, local_imports
from vwb.workspace.projects import ProjectStore
from vwb.workspace.registry import Registry
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def project(tmp_path):
    store = ProjectStore(tmp_path)
    row = store.create("Shared files")
    workspaces = []
    for name in ("r-one", "r-two"):
        ws = Workspace(tmp_path / name)
        for sub in SUBDIRS:
            (ws.root / sub).mkdir(parents=True, exist_ok=True)
        ws.save(Request(id=name, message="Project files"))
        store.move(ws, row["id"])
        # This is a real, now-inactive folder, never a symlink to shared work.
        (ws.root / "work" / "legacy.txt").write_text("Old conversation copy")
        workspaces.append(ws)
    return workspaces


@pytest.fixture
def imports():
    service = local_imports.LocalImports()
    try:
        yield service
    finally:
        service.close()


def imported(service, ws, source):
    identity = service.start(ws, str(source))["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = service.get(ws, identity)
        if row["status"] in local_imports.TERMINAL:
            return row
        time.sleep(.01)
    raise AssertionError("Local import did not finish")


def png():
    stream = io.BytesIO()
    Image.new("RGBA", (32, 24), (20, 40, 60, 255)).save(stream, format="PNG")
    return stream.getvalue()


class TransferDriver:
    def __init__(self, before=None):
        self.before = before
        self.calls = []
        self.uploaded = None
        self.downloaded = b"Project download"

    async def run(self, args, **kwargs):
        self.calls.append(args)
        if self.before:
            self.before(args)
        if args[0] == "upload":
            self.uploaded = Path(args[2]).read_bytes()
        elif args[0] == "download":
            Path(args[2]).write_bytes(self.downloaded)
        return {"ok": True}


def command(session, action, **body):
    return session._command({}, {"action": action, "ref": "e1", **body}, actor="job_lead")


@pytest.mark.parametrize("folder", [False, True])
def test_imports_publish_shared_independent_copies_with_private_manifests(project, imports, tmp_path, folder):
    ws, peer = project
    source = tmp_path / "Selected"
    if folder:
        source.mkdir()
        selected = source / "nested" / "input.txt"
        selected.parent.mkdir()
    else:
        selected = source
    selected.write_bytes(b"Selected source")
    row = imported(imports, ws, source)
    assert row["status"] == "ready", row
    relative = row["reference"]["id"] + ("/nested/input.txt" if folder else "")
    target = ws.work / relative
    assert target.read_bytes() == b"Selected source"
    assert target.stat().st_ino != selected.stat().st_ino
    assert (peer.work / relative).read_bytes() == b"Selected source"
    target.write_bytes(b"Project edit")
    assert selected.read_bytes() == b"Selected source"
    assert not (ws.root / "work" / "imports").exists()
    assert (ws.root / ".local-imports" / row["id"] / "manifest.json").is_file()
    assert not (ws.project_root / ".local-imports").exists()
    assert imports.list(peer) == {"jobs": []}
    assert row["reference"]["name"].startswith("work/imports/")


def test_import_rejects_symlink_in_shared_destination(project, imports, tmp_path):
    ws, _ = project
    source = tmp_path / "input.txt"
    source.write_text("source")
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws.work / "imports").symlink_to(outside, target_is_directory=True)
    row = imported(imports, ws, source)
    assert row["status"] == "failed" and "reference" not in row
    assert list(outside.iterdir()) == []
    assert not (ws.root / "work" / "imports").exists()


@pytest.mark.parametrize("component", ["work", "imports"])
def test_import_detects_shared_directory_swap_and_removes_pinned_copy(project, imports, tmp_path, monkeypatch, component):
    ws, _ = project
    work = ws.work
    source = tmp_path / "input.txt"
    source.write_text("source")
    outside = tmp_path / "outside"
    outside.mkdir()
    original = local_imports._rename_new
    target = work if component == "work" else work / "imports"
    moved = target.with_name("moved-" + component)

    def swap(*args):
        target.rename(moved)
        target.symlink_to(outside, target_is_directory=True)
        return original(*args)

    monkeypatch.setattr(local_imports, "_rename_new", swap)
    row = imported(imports, ws, source)
    assert row["status"] == "failed" and "folder changed" in row["error"]
    assert list(outside.iterdir()) == []
    assert list((moved / "imports" if component == "work" else moved).iterdir()) == []


def test_studio_upload_and_preparation_share_files_but_keep_receipts_private(project):
    ws, peer = project
    data = png()
    uploaded = assets.upload(ws, io.BytesIO(data), len(data), name="source.png", kind="image")
    assert ws.path(uploaded["path"]).read_bytes() == peer.path(uploaded["path"]).read_bytes()
    spec = JobInput.model_validate({"kind": "image", "operation": "edit", "prompt": "A blue sphere",
                                   "idempotency_key": "project-edit-0001", "inputs": [{"upload_id": uploaded["upload_id"]}]})
    prepared = prepare(ws, spec, "a" * 32)
    snapshot = prepared["prepared"]["inputs"][0]
    assert prepared["output_folder"] == "work/studio/images/" + "a" * 32
    assert snapshot["path"] == prepared["output_folder"] + "/inputs/0.png"
    assert ws.path(snapshot["path"]).read_bytes() == peer.path(snapshot["path"]).read_bytes()
    assert Path(Registry(ws.assets).get(snapshot["ref"])["path"]).is_relative_to(ws.assets)
    assert private_job(ws, "a" * 32).is_relative_to(ws.root / ".studio")
    assert (ws.root / ".studio" / "uploads" / f"{uploaded['upload_id']}.json").is_file()
    assert not (ws.project_root / ".studio").exists()
    assert not (peer.root / ".studio").exists()
    assert not (ws.root / "work" / "studio").exists()
    # A second conversation can prepare from the shared logical path.
    spec = JobInput.model_validate({**spec.model_dump(), "inputs": [{"path": uploaded["path"]}]})
    assert prepare(peer, spec, "b" * 32)["prepared"]["inputs"][0]["source"] == uploaded["path"]


@pytest.mark.parametrize("component", ["work", "studio", "file"])
def test_studio_rejects_symlinked_shared_publication(project, tmp_path, component):
    ws, _ = project
    work = ws.work
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "work":
        work.rmdir()
        work.symlink_to(outside, target_is_directory=True)
    elif component == "studio":
        (work / "studio").symlink_to(outside, target_is_directory=True)
    else:
        (work / "studio").mkdir()
        (work / "studio" / "output.png").symlink_to(outside / "output.png")
    with pytest.raises((ValueError, OSError)):
        assets.publish(ws, "work/studio/output.png", b"output")
    assert list(outside.iterdir()) == []
    assert not (ws.root / "work" / "studio").exists()


def test_studio_publication_keeps_pinned_directory_after_swap(project, tmp_path, monkeypatch):
    ws, _ = project
    work = ws.work
    (work / "studio").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = os.open

    def swap(path, flags, *args, **kwargs):
        if path == "output.png":
            (work / "studio").rename(work / "moved-studio")
            (work / "studio").symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    assets.publish(ws, "work/studio/output.png", b"output")
    assert list(outside.iterdir()) == []
    assert (work / "moved-studio" / "output.png").read_bytes() == b"output"


@pytest.mark.parametrize("root", ["work", "media", "assets"])
def test_browser_upload_pins_selected_file_and_keeps_daemon_snapshot_private(project, tmp_path, root):
    ws, _ = project
    selected = ws.path(f"{root}/selected.txt")
    selected.write_bytes(b"Selected bytes")
    if root == "work":
        (ws.root / "work" / "selected.txt").write_bytes(b"Inactive conversation file")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Never upload this")

    def swap(args):
        selected.unlink()
        selected.symlink_to(outside)
        assert Path(args[2]).is_relative_to(ws.root / ".browser")
        assert Path(args[2]).name == "selected.txt"

    driver = TransferDriver(swap)
    session = BrowserSession(ws, driver=driver)
    assert command(session, "upload", path=f"{root}/selected.txt") == {"ok": True}
    assert driver.uploaded == b"Selected bytes"
    # File inputs may defer reading until a later form submit or browser action.
    BrowserSession(ws, driver=driver)
    assert Path(driver.calls[0][2]).read_bytes() == b"Selected bytes"
    assert not (ws.project_root / ".browser").exists()


@pytest.mark.parametrize("component", ["work", "directory", "file"])
def test_browser_upload_rejects_symlinks_in_shared_work(project, tmp_path, component):
    ws, _ = project
    work = ws.work
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "input.txt").write_text("private")
    relative = "work/input.txt"
    if component == "work":
        work.rmdir()
        work.symlink_to(outside, target_is_directory=True)
    elif component == "directory":
        (work / "linked").symlink_to(outside, target_is_directory=True)
        relative = "work/linked/input.txt"
    else:
        (work / "input.txt").symlink_to(outside / "input.txt")
    driver = TransferDriver()
    session = BrowserSession(ws, driver=driver)
    with pytest.raises(ValueError):
        command(session, "upload", path=relative)
    assert driver.calls == []


def test_browser_upload_does_not_fall_back_to_inactive_conversation_work(project):
    ws, _ = project
    driver = TransferDriver()
    with pytest.raises(ValueError):
        command(BrowserSession(ws, driver=driver), "upload", path="work/legacy.txt")
    assert driver.calls == []
    assert (ws.root / "work" / "legacy.txt").read_text() == "Old conversation copy"


def test_browser_download_is_shared_with_logical_path_and_private_runtime(project):
    ws, peer = project
    driver = TransferDriver()
    session = BrowserSession(ws, driver=driver)
    result = command(session, "download", filename="result.txt")
    assert result["path"].startswith("work/browser-downloads/")
    assert result["bytes"] == len(driver.downloaded)
    assert result["sha256"] == hashlib.sha256(driver.downloaded).hexdigest()
    assert peer.path(result["path"]).read_bytes() == driver.downloaded
    assert not (ws.root / "work" / "browser-downloads").exists()
    assert list((session.private / "downloads").iterdir()) == []
    assert (session.private / "state.json").is_file()
    assert not (ws.project_root / ".browser").exists()
    assert not (peer.root / ".browser").exists()


@pytest.mark.parametrize("component", ["work", "browser-downloads"])
def test_browser_download_rejects_symlinked_shared_destination(project, tmp_path, component):
    ws, _ = project
    work = ws.work
    outside = tmp_path / "outside"
    outside.mkdir()
    target = work if component == "work" else work / component
    if component == "work":
        target.rmdir()
    target.symlink_to(outside, target_is_directory=True)
    driver = TransferDriver()
    with pytest.raises(ValueError):
        command(BrowserSession(ws, driver=driver), "download", filename="result.txt")
    assert driver.calls == []
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("component", ["work", "browser-downloads"])
def test_browser_download_rejects_directory_swap_and_cleans_pinned_output(project, tmp_path, component):
    ws, _ = project
    work = ws.work
    outside = tmp_path / "outside"
    outside.mkdir()
    target = work if component == "work" else work / component
    moved = target.with_name("moved-" + component)

    def swap(args):
        target.rename(moved)
        target.symlink_to(outside, target_is_directory=True)

    driver = TransferDriver(swap)
    session = BrowserSession(ws, driver=driver)
    with pytest.raises(ValueError, match="folder changed"):
        command(session, "download", filename="result.txt")
    assert list(outside.iterdir()) == []
    assert list((moved / "browser-downloads" if component == "work" else moved).iterdir()) == []
    assert list((session.private / "downloads").iterdir()) == []


def test_browser_download_does_not_replace_existing_shared_file(project, monkeypatch):
    ws, _ = project
    monkeypatch.setattr(browser_service.secrets, "token_hex", lambda _: "fixed")
    folder = ws.work / "browser-downloads"
    folder.mkdir()
    target = folder / "fixed-result.txt"
    target.write_bytes(b"Keep this")
    session = BrowserSession(ws, driver=TransferDriver())
    with pytest.raises(FileExistsError):
        command(session, "download", filename="result.txt")
    assert target.read_bytes() == b"Keep this"
    assert list((session.private / "downloads").iterdir()) == []


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo"])
def test_browser_download_rejects_missing_or_nonregular_source(project, tmp_path, kind):
    ws, _ = project
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Private source")

    class InvalidDownload(TransferDriver):
        async def run(self, args, **kwargs):
            if kind == "symlink":
                Path(args[2]).symlink_to(outside)
            elif kind == "fifo":
                os.mkfifo(args[2])
            return {"ok": True}

    session = BrowserSession(ws, driver=InvalidDownload())
    with pytest.raises(ValueError, match="regular file"):
        command(session, "download", filename="result.txt")
    assert outside.read_bytes() == b"Private source"
    assert list((ws.work / "browser-downloads").iterdir()) == []
    assert list((session.private / "downloads").iterdir()) == []


@pytest.mark.parametrize("action", ["upload", "download"])
def test_browser_transfer_size_bound_applies_to_shared_files(project, monkeypatch, action):
    ws, _ = project
    monkeypatch.setattr(browser_service, "TRANSFER_MAX", 3)
    (ws.work / "large.txt").write_bytes(b"Too large")
    session = BrowserSession(ws, driver=TransferDriver())
    with pytest.raises(ValueError, match="128 MB"):
        command(session, action, path="work/large.txt", filename="result.txt")
    assert not list(session.private.glob("upload-*"))
    if action == "download":
        assert list((ws.work / "browser-downloads").iterdir()) == []
        assert list((session.private / "downloads").iterdir()) == []
