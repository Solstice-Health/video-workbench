"""Fork/rewind/edit public chat at a stable message identity.

HTTP integration: ``apply(source, destination_root, body)`` returns
``{workspace: Workspace, result: dict, retained_refs: list[dict]}``.
``destination_root`` is the exact new workspace path for fork/edit-with-fork,
otherwise None. ``result.edited_message`` is an enqueue payload with text,
context, attachments and replaces_message_id; this module does not enqueue or
append the replacement. The caller must serialize mutation and enqueue with
its idle guard and deduplicate that enqueue by operation_id, including retries.

Only public chat is historical. Working files and output versions are current;
project forks share work, standalone forks copy work using independent CoW
clones when available. Credentials, browser sessions and private QC are never
part of the copy allowlist. Message and tool filesystem snapshots are not made.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from vwb.util.hashing import sha256_file
from vwb.util.jsonio import read_json
from vwb.workspace import attachments, checkpoints
from vwb.workspace.checkpoints import HistoryConflict, checked_path, durable_json
from vwb.workspace.local_imports import _copy_file, _rename_new, _stamp
from vwb.workspace.request import SUBDIRS, ChatTurn, Request, Workspace, now_iso

_ROLES = {"human", "job_lead", "assistant"}
_ARTIFACTS = ("assets", "canvas", "copy", "renders", "plates")
_WORK_EXCLUDED = {
    ".git", ".env", ".aws", ".ssh", ".codex", ".claude", ".cursor", ".keys", ".browser",
    ".qc-chat", ".qc", ".integrations", ".sandbox", ".results", ".cache", ".venv", "venv",
    "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".npmrc", ".netrc",
    "credentials.json", "auth.json", "cookies.json", "storage_state.json",
}


def _excluded(name: str) -> bool:
    lower = name.lower()
    return (lower in _WORK_EXCLUDED or lower.startswith(".env.")
            or lower.endswith((".pem", ".key")))


def _copy_tree(source: Path, destination: Path, *, work: bool = False, skipped: list[str] | None = None) -> None:
    """Pin every traversed directory/file; never follow agent-created symlinks."""
    checked_path(source)
    checked_path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    cancel = threading.Event()
    entries = 0

    def walk(incoming: int, outgoing: int, relative: Path) -> None:
        nonlocal entries
        if len(relative.parts) > 64:
            raise ValueError("The workspace is too deeply nested to fork safely.")
        with os.scandir(incoming) as children:
            for child in children:
                entries += 1
                if entries > 200_000:
                    raise ValueError("The workspace exceeds the fork file limit; remove generated caches first.")
                rel = relative / child.name
                info = child.stat(follow_symlinks=False)
                if work and (_excluded(child.name) or stat.S_ISLNK(info.st_mode)):
                    if skipped is not None:
                        skipped.append(rel.as_posix())
                    continue
                if stat.S_ISDIR(info.st_mode):
                    os.mkdir(child.name, dir_fd=outgoing)
                    src = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=incoming)
                    try:
                        dst = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=outgoing)
                        try:
                            walk(src, dst, rel)
                            os.fsync(dst)
                        finally:
                            os.close(dst)
                    finally:
                        os.close(src)
                elif stat.S_ISREG(info.st_mode):
                    src = os.open(child.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=incoming)
                    try:
                        _copy_file(src, outgoing, child.name, _stamp(info), cancel, lambda _: None)
                        os.chmod(child.name, stat.S_IMODE(info.st_mode) & 0o777, dir_fd=outgoing)
                        dst = os.open(child.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=outgoing)
                        try:
                            os.fsync(dst)
                        finally:
                            os.close(dst)
                    finally:
                        os.close(src)
                else:
                    raise ValueError(f"Only regular files/directories can be forked: {rel}")

    src = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        dst = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            walk(src, dst, Path())
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)


def _copy_one(source: Path, destination: Path) -> None:
    checked_path(source)
    checked_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        dst = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _copy_file(src, dst, destination.name, _stamp(os.fstat(src)), threading.Event(), lambda _: None)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)


def _select(req: Request, message_id: str | None) -> int:
    ids = [turn.id for turn in req.chat]
    if any(not isinstance(ident, str) or not ident for ident in ids) or len(set(ids)) != len(ids):
        raise ValueError("Conversation messages must have unique, nonempty identities.")
    if any(turn.role not in _ROLES for turn in req.chat):
        raise ValueError("Only human and assistant message checkpoints are supported.")
    if message_id is None:
        return len(req.chat) - 1
    try:
        return ids.index(message_id)
    except ValueError:
        raise ValueError("That message is not in this conversation.") from None


def _refs(ws: Workspace, turns: list[ChatTurn]) -> list[dict]:
    refs = {}
    for turn in turns:
        for ref in turn.attachments:
            row = attachments.canonical(ws, ref)
            # Verify actual bytes before any destructive history operation.
            attachments.original_path(ws, row)
            refs[row["id"]] = row
    return list(refs.values())


def _copy_sources(source: Workspace, staged: Workspace, target: Path, req: Request) -> None:
    for item in req.sources:
        original = Path(item.path)
        if not original.is_absolute():
            original = source.root / original
        original = checked_path(original)
        try:
            relative = original.relative_to(checked_path(source.media))
        except ValueError:
            raise ValueError("Source media must reside in the conversation's media store.") from None
        destination = staged.media / relative
        _copy_one(original, destination)
        if sha256_file(destination) != item.sha256:
            raise ValueError("Source media failed its integrity check.")
        item.path = str(target / "media" / relative)
        # Internal provenance paths must not keep a dependency on the source.
        try:
            provenance = Path(item.original_path).relative_to(source.root)
        except ValueError:
            pass
        else:
            item.original_path = str(target / provenance)


def _snapshot_databases(source: Workspace, staged: Workspace, target: Path) -> None:
    """SQLite backup captures WAL data consistently and rebases operational paths."""
    paths = [source.assets / "registry.sqlite", *source.index.rglob("*.sqlite")]
    for original in paths:
        if not original.exists():
            continue
        original = checked_path(original)
        destination = staged.root / original.relative_to(source.root)
        if not destination.exists():
            continue
        # The copied sidecars are disposable; replace the copy with a consistent
        # read transaction from the idle source, even if SQLite has a live WAL.
        for suffix in ("", "-wal", "-shm"):
            Path(str(destination) + suffix).unlink(missing_ok=True)
        with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                tables = {row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("revisions", "media"):
                    if table not in tables:
                        continue
                    for rowid, value in dst.execute(f"SELECT rowid, path FROM {table}").fetchall():
                        if not value:
                            continue
                        try:
                            relative = Path(value).relative_to(source.root)
                        except ValueError:
                            continue
                        dst.execute(f"UPDATE {table} SET path=? WHERE rowid=?", (str(target / relative), rowid))
                dst.commit()
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())


def _rebase_receipts(source: Workspace, staged: Workspace, target: Path) -> None:
    def rebase(value):
        if isinstance(value, dict):
            return {key: rebase(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebase(item) for item in value]
        if isinstance(value, str) and value.startswith(str(source.root) + "/"):
            return str(target / Path(value).relative_to(source.root))
        return value

    # Render receipts are operational pointers to copied bytes. Immutable
    # canvas documents and their provenance hashes remain byte-for-byte intact.
    for receipt in staged.renders.rglob("receipt.json"):
        durable_json(receipt, rebase(read_json(receipt)))


def _discard_staging(record: dict) -> None:
    """Remove only the private staging directory this operation created."""
    if not record.get("staging"):
        return
    staged = checked_path(Path(record["staging"]))
    target = checked_path(Path(record["destination"]))
    if staged.parent != target.parent or not staged.name.startswith(f".{target.name}-fork-"):
        raise HistoryConflict("Invalid fork recovery directory; its files have been left intact.")
    if staged.exists():
        shutil.rmtree(staged)


def _publish_fork(source: Workspace, target: Path, req: Request, refs: list[dict], record: dict,
                  record_path: Path) -> dict:
    target = checked_path(target)
    if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", target.name)
            or target == source.root.resolve() or target.is_relative_to(source.root.resolve())
            or source.root.resolve().is_relative_to(target)
            or target.is_relative_to(source.work.resolve())):
        raise ValueError("Choose a separate, valid conversation destination.")
    if target.exists():
        origin = read_json(checked_path(target / checkpoints.STORE / "origin.json"))
        if origin and origin.get("identity") == record["identity"]:
            return origin["result"]
        raise HistoryConflict("The conversation destination already exists.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{target.name}-fork-", dir=target.parent))
    staged = Workspace(temp)
    try:
        record["staging"] = str(temp)
        durable_json(record_path, record)
        for name in SUBDIRS:
            (temp / name).mkdir()
        skipped: list[str] = []
        if source.project_root is None:
            _copy_tree(source.work, staged.work, work=True, skipped=skipped)
        _copy_sources(source, staged, target, req)
        for ref in refs:
            folder = staged.root / ".attachments" / ref["id"]
            _copy_one(attachments.original_path(source, ref), folder / "original")
            (folder / "original").chmod(0o444)
            durable_json(folder / "metadata.json", ref)
            # SourceRef.original_path may name the pinned video's source alias.
            if ref["kind"] == "video":
                alias = folder / "source" / attachments.filename(ref["name"])
                alias.parent.mkdir()
                os.link(folder / "original", alias)
        # Current editable outputs keep their immutable inputs and versions.
        # No release acceptance, QC, browser, provider or job-state is cloned.
        for name in _ARTIFACTS:
            directory = source.root / name
            if directory.exists():
                _copy_tree(directory, staged.root / name)
        # Source indexing is a cache. Preserve it only if the source set agrees.
        if source.index.exists() and [s.ref for s in req.sources] == [s.ref for s in source.load().sources]:
            _copy_tree(source.index, staged.index)
        _snapshot_databases(source, staged, target)
        _rebase_receipts(source, staged, target)
        req.id, req.created = target.name, now_iso()
        durable_json(staged.request_path, req.model_dump(mode="json"))
        if source.project_root is not None:
            from vwb.workspace.projects import ProjectStore

            ProjectStore(source.project_root.parent.parent).bind_clone(source, staged)
        checkpoints.initialize_context(staged, req)
        result = {**record["result"], "skipped_work_paths": skipped,
                  "work_mode": "shared_project" if source.project_root else "copied_current"}
        durable_json(staged.root / checkpoints.STORE / "origin.json",
                     {"identity": record["identity"], "result": result})
        checkpoints.sync_directory(temp)
        parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _rename_new(parent, temp.name, parent, target.name)
            os.fsync(parent)
        finally:
            os.close(parent)
        return result
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def apply(source: Workspace, destination_root: Path | None, body: dict[str, Any]) -> dict[str, Any]:
    """Apply the HTTP history payload under the caller's idle/mutation guard.

    Keys: action, message_id (None/latest is supported for fork/rewind), text
    (edit only), fork (edit only), expected_revision, operation_id. Reusing the
    same operation_id returns its original outcome, even after newer chat; a
    different payload with that identity conflicts. On a retry the proposed
    destination is ignored in favor of the previously recorded destination.
    """
    if not isinstance(body, dict) or set(body) - {
        "action", "message_id", "text", "fork", "expected_revision", "operation_id",
    }:
        raise ValueError("Invalid conversation history operation.")
    action = body.get("action")
    if action not in {"fork", "rewind", "edit"}:
        raise ValueError("Choose fork, rewind or edit.")
    message_id = body.get("message_id")
    if message_id is not None and (not isinstance(message_id, str) or not message_id or len(message_id) > 200):
        raise ValueError("Invalid message identity.")
    if "fork" in body and not isinstance(body["fork"], bool):
        raise ValueError("fork must be a boolean.")
    if action != "edit" and ("text" in body or body.get("fork")):
        raise ValueError("Only edit accepts text and fork options.")
    if action == "edit" and (message_id is None or not isinstance(body.get("text"), str)
                             or len(body["text"]) > 100_000):
        raise ValueError("Editing requires a message identity and text of at most 100,000 characters.")
    revision = body.get("expected_revision")
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError("expected_revision must be a nonnegative integer.")
    op_id = body.get("operation_id") if "operation_id" in body else uuid.uuid4().hex
    copying = action == "fork" or (action == "edit" and body.get("fork", False))
    identity = {"source": str(source.root.resolve()), "operation_id": op_id,
                "action": action, "message_id": message_id, "text": body.get("text"),
                "fork": copying, "expected_revision": revision}
    with checkpoints.lock(source):
        path = checkpoints.operation_path(source, op_id)
        existing = read_json(path)
        if existing:
            if existing["identity"] != identity:
                raise HistoryConflict("That operation_id already belongs to a different history operation.")
            if copying and existing["status"] != "complete":
                published = checked_path(Path(existing["destination"]) / checkpoints.STORE / "origin.json")
                origin = read_json(published)
                if origin and origin.get("identity") == identity:
                    existing.update(status="complete", result=origin["result"])
                    durable_json(path, existing)
                else:
                    _discard_staging(existing)
                    existing["status"] = "failed"
                    durable_json(path, existing)
            if existing["status"] == "prepared":
                checkpoints.finish_reset(source, path, existing)
            if existing["status"] == "complete":
                ws = Workspace(Path(existing["destination"])) if copying else source
                if not ws.request_path.is_file():
                    raise HistoryConflict("The original operation's conversation is no longer available.")
                return {"workspace": ws, "result": {**existing["result"], "replayed": True},
                        "retained_refs": existing["retained_refs"]}
        # A different mutation cannot bypass a partially completed reset.
        for candidate in path.parent.parent.glob("*/operation.json"):
            other = read_json(checked_path(candidate))
            if candidate != path and other and other.get("status") in {"prepared", "copying"}:
                raise HistoryConflict("An unfinished history operation must be retried before another change.")
        before = source.load()
        if revision is not None and revision != before.history_revision:
            raise HistoryConflict("Conversation history changed. Reload before retrying this operation.")
        index = _select(before, message_id)
        selected = before.chat[index] if index >= 0 else None
        if action == "edit" and (selected is None or selected.role != "human"):
            raise ValueError("Only a human message can be edited.")
        if action == "edit" and not body["text"].strip() and not selected.attachments:
            raise ValueError("An edited message needs text or an attachment.")
        keep = index if action == "edit" else index + 1
        after = before.model_copy(deep=True)
        after.chat = after.chat[:keep]
        after.spec, after.status = None, "open"
        after.history_revision = before.history_revision + 1
        after.message = next((turn.text for turn in after.chat if turn.role == "human"), "")
        ref_turns = before.chat[:index + 1]
        refs = _refs(source, ref_turns)
        edited = ({"text": body["text"], "context": copy.deepcopy(selected.context),
                   "attachments": copy.deepcopy(selected.attachments), "replaces_message_id": selected.id}
                  if action == "edit" else None)
        target = (Path(existing["destination"]) if existing else
                  checked_path(destination_root) if copying and destination_root is not None else None)
        if copying and target is None:
            raise ValueError("Forking requires a new conversation destination.")
        result = {"action": action, "operation_id": op_id, "request_id": target.name if copying else source.id,
                  "source_request_id": source.id, "message_id": selected.id if selected else None,
                  "history_revision": after.history_revision, "retained_messages": keep,
                  "removed_messages": len(before.chat) - keep, "edited_message": edited,
                  "backup_path": None if copying else str(path.parent), "replayed": False,
                  "filesystem": "current", "provider_context": "fresh",
                  "notice": "Conversation history changed. Working files and output versions remain at their current state."}
        record = {"schema": 1, "identity": identity, "destination": str(target) if target else None,
                  "result": result, "retained_refs": refs}
        if copying:
            record["status"] = "copying"
            durable_json(path, record)
            try:
                record["result"] = _publish_fork(source, target, after, refs, record, path)
            except BaseException:
                # Leave successful publication recoverable if only recording
                # its receipt failed; failures before publication may retry.
                record["status"] = "failed"
                durable_json(path, record)
                raise
            record["status"] = "complete"
            durable_json(path, record)
            return {"workspace": Workspace(target), "result": record["result"], "retained_refs": refs}
        checkpoints.prepare_reset(source, path, record, before, after)
        result = checkpoints.finish_reset(source, path, record)
        return {"workspace": source, "result": result, "retained_refs": refs}


def fork_conversation(source: Workspace, target: Path, message_id: str | None = None, **options) -> Workspace:
    return apply(source, target, {"action": "fork", "message_id": message_id, **options})["workspace"]


def rewind_conversation(source: Workspace, message_id: str | None = None, **options) -> dict:
    return apply(source, None, {"action": "rewind", "message_id": message_id, **options})


def edit_conversation(source: Workspace, message_id: str, text: str, *, target: Path | None = None,
                      **options) -> dict:
    return apply(source, target, {"action": "edit", "message_id": message_id, "text": text,
                                 "fork": target is not None, **options})
