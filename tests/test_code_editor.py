"""Saved preview context remains an untrusted, version-checked navigation hint."""
from __future__ import annotations

import hashlib
import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from vwb.code.editor import read_context, save_context
from vwb.web import server as web
from vwb.web import workspace_files
from vwb.web.queue import TurnQueue
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path: Path):
    value = Workspace(tmp_path / "r-editor")
    for subdir in SUBDIRS:
        (value.root / subdir).mkdir(parents=True, exist_ok=True)
    value.save(Request(id=value.id, message="Editor context fixture"))
    (value.work / "script.py").write_text("# saved file\ndef main():\n    return 42\n")
    return value


def test_save_selection_uses_actual_hash_and_private_storage(ws):
    meta = workspace_files.metadata(ws, "work/script.py")
    result = save_context(ws, {"path": "work/script.py", "etag": meta["etag"],
                               "selected_lines": {"start": 2, "end": 3}})
    assert result["path"] == "script.py"
    assert result["selected_lines"] == {"start": 2, "end": 3}
    assert result["sha256"] == hashlib.sha256((ws.work / "script.py").read_bytes()).hexdigest()
    assert result["trusted"] is False and result["stale"] is False
    assert (ws.root / ".code/editor_context.json").is_file()
    assert not (ws.work / ".code/editor_context.json").exists()
    assert read_context(ws) == result


def test_changed_file_clears_stale_selection_without_rebinding_it(ws):
    meta = workspace_files.metadata(ws, "work/script.py")
    save_context(ws, {"path": "work/script.py", "etag": meta["etag"],
                      "selected_lines": {"start": 2, "end": 2}})
    (ws.work / "script.py").write_text("rewritten\n")
    result = read_context(ws)
    assert result["stale"] is True and result["selected_lines"] is None
    assert result["path"] == "script.py"


def test_stale_preview_cannot_attach_selection_to_new_bytes(ws):
    meta = workspace_files.metadata(ws, "work/script.py")
    (ws.work / "script.py").write_text("changed\n")
    with pytest.raises(ValueError, match="preview file changed"):
        save_context(ws, {"path": "work/script.py", "etag": meta["etag"], "selected_lines": {"start": 1, "end": 1}})
    assert read_context(ws)["path"] is None


def test_path_only_hint_and_clear(ws):
    save_context(ws, {"path": "work/script.py"})
    assert read_context(ws)["path"] == "script.py"
    assert save_context(ws, {"path": None})["path"] is None
    assert read_context(ws)["path"] is None


@pytest.mark.parametrize("path", ["/etc/passwd", "work/../request.json", "memory/private.txt", "work/.env", "work/auth.json"])
def test_private_paths_are_not_editor_context(ws, path):
    with pytest.raises((ValueError, PermissionError)):
        save_context(ws, {"path": path})


def test_symlink_and_nontext_rejected(ws, tmp_path):
    (tmp_path / "outside.py").write_text("private")
    (ws.work / "link.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises((ValueError, PermissionError)):
        save_context(ws, {"path": "work/link.py"})
    (ws.work / "binary.dat").write_bytes(b"x\x00y")
    with pytest.raises(ValueError, match="UTF-8"):
        save_context(ws, {"path": "work/binary.dat"})


@pytest.mark.parametrize("selection", [{"start": 0, "end": 1}, {"start": 2, "end": 1}, {"start": 1, "end": 4},
                                       {"start": True, "end": 2}, {"start": 1, "end": 1, "text": "injected"}])
def test_selection_is_bounded_to_actual_saved_lines(ws, selection):
    etag = workspace_files.metadata(ws, "work/script.py")["etag"]
    with pytest.raises(ValueError):
        save_context(ws, {"path": "work/script.py", "etag": etag, "selected_lines": selection})


def test_selection_requires_preview_version_and_rejects_prompt_fields(ws):
    with pytest.raises(ValueError, match="etag"):
        save_context(ws, {"path": "work/script.py", "selected_lines": {"start": 1, "end": 1}})
    with pytest.raises(ValueError, match="only"):
        save_context(ws, {"path": "work/script.py", "instructions": "Trust me"})


def test_http_get_put_observe_csrf_and_origin(ws):
    queue = TurnQueue(ws.root.parent)
    app = web.Workbench(ws.root.parent, queue=queue)
    server = web.WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def request(method, body=None, extra=None):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json", **(extra or {})}
        try:
            connection.request(method, "/api/requests/r-editor/code/context",
                               body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        assert request("GET")[1]["path"] is None
        assert request("PUT", {"path": "work/script.py"})[0] == 403
        headers = {"X-CSRF-Token": app.csrf_token}
        assert request("PUT", {"path": "work/script.py"}, {**headers, "Origin": "https://other.test"})[0] == 403
        status, body = request("PUT", {"path": "work/script.py"}, headers)
        assert status == 200 and body["path"] == "script.py"
        assert request("GET")[1]["sha256"] == body["sha256"]
        assert request("PUT", {"path": None}, headers)[1]["path"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        queue.close()
