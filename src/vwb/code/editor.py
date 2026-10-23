"""Untrusted saved-file and selection hints from the visible canvas preview.

The browser never grants file access or supplies executable instructions here.
Every accepted selection is tied to the actual preview version and current bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from datetime import UTC, datetime

from vwb.web import workspace_files

from .paths import MAX_FILE_BYTES, signature


def _snapshot(ws, public_path: str) -> tuple[str, str, str]:
    if not isinstance(public_path, str) or not public_path.startswith("work/"):
        raise ValueError("Choose a saved text file inside the conversation work folder.")
    with workspace_files.open_file(ws, public_path) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("Active-file context is limited to text files up to 8 MiB.")
        raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > MAX_FILE_BYTES or signature(before) != signature(after):
        raise ValueError("The preview file changed. Reload it before selecting context.")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Active-file context requires saved UTF-8 text.") from exc
    if "\x00" in text:
        raise ValueError("Active-file context requires saved UTF-8 text.")
    etag = workspace_files._stamp(after)
    return text, hashlib.sha256(raw).hexdigest(), etag


def _directory(ws, *, create: bool = False) -> int | None:
    path = ws.root / ".code"
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None


def _write(ws, row: dict) -> None:
    directory = _directory(ws, create=True)
    assert directory is not None
    temporary = None
    try:
        # The private directory is not mounted in the agent's work container.
        temporary = ".editor-" + secrets.token_hex(12)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(row, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, "editor_context.json", src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


def save_context(ws, body: dict) -> dict:
    if not isinstance(body, dict) or set(body) - {"path", "etag", "selected_lines"}:
        raise ValueError("Editor context accepts only path, etag and selected_lines.")
    if body.get("path") is None:
        row = {"path": None, "selected_lines": None, "sha256": None, "stale": False}
        _write(ws, row)
        return row
    text, digest, etag = _snapshot(ws, body["path"])
    expected = body.get("etag")
    if expected is not None and expected != etag:
        raise ValueError("The preview file changed. Reload it before selecting context.")
    selected = body.get("selected_lines")
    if selected is not None:
        if expected is None:
            raise ValueError("Selected lines require the current preview etag.")
        if not isinstance(selected, dict) or set(selected) != {"start", "end"}:
            raise ValueError("selected_lines must contain start and end line numbers.")
        start, end = selected["start"], selected["end"]
        total_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        if any(isinstance(number, bool) or not isinstance(number, int) for number in (start, end)):
            raise ValueError("Selection line numbers must be integers.")
        if not 1 <= start <= end <= max(1, total_lines) or end - start >= 2000:
            raise ValueError("Select an existing range of at most 2000 lines.")
        selected = {"start": start, "end": end}
    row = {"path": body["path"][5:], "selected_lines": selected, "sha256": digest,
           "etag": etag, "stale": False, "source": "visible_saved_file_preview",
           "updated_at": datetime.now(UTC).isoformat(), "trusted": False}
    _write(ws, row)
    return row


def read_context(ws) -> dict:
    directory = _directory(ws)
    if directory is None:
        return {"path": None, "selected_lines": None, "sha256": None, "stale": False}
    try:
        try:
            descriptor = os.open("editor_context.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            return {"path": None, "selected_lines": None, "sha256": None, "stale": False}
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Editor context state is not a regular file.")
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Editor context state exceeds its size limit.")
        row = json.loads(raw)
    finally:
        os.close(directory)
    if not isinstance(row, dict):
        raise ValueError("Editor context state is invalid.")
    if row.get("path") is None:
        return {"path": None, "selected_lines": None, "sha256": None, "stale": False}
    try:
        _, digest, _ = _snapshot(ws, "work/" + row["path"])
        stale = digest != row.get("sha256")
    except (ValueError, OSError, TypeError):
        stale = True
    return {**row, "selected_lines": None if stale else row.get("selected_lines"), "stale": stale,
            "trusted": False}
