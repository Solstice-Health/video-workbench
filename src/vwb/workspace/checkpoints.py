"""Message checkpoints and recoverable provider-context resets.

These are *conversation* checkpoints: work files, output versions, the signed
ledger and private QC records are not rolled back. A reset quarantines derived
context before publishing the new public chat. The HTTP caller must exclude
running/queued turns for the entire operation (including an edit's enqueue).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from filelock import FileLock

from vwb.util.jsonio import read_json, write_json
from vwb.workspace.request import Request, Workspace

STORE = ".conversation-history"
CHECKPOINT = "conversation_checkpoint.json"
# These hold UI choices, queued-job receipts, or current artifacts, not the main
# agent's continuation. Everything else in memory is quarantined on a reset.
KEEP_MEMORY = {
    "model_selection.json", "prompt_selection.json", "web_queue.json", "web_jobs",
    "artifacts", "submissions.json", "qc_sessions.json", "last_qc_model.json", "qc_inspections.jsonl",
}


class HistoryConflict(ValueError):
    """A stale revision, a reused operation key, or an unfinished operation."""


def checked_path(path: Path) -> Path:
    """Do not traverse symlinks in control-plane stores or publication paths."""
    path = Path(os.path.abspath(path))
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise ValueError(f"Conversation history paths cannot contain symbolic links: {parent.name}")
    return path


def store(ws: Workspace) -> Path:
    root = checked_path(ws.root / STORE)
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def lock(ws: Workspace) -> FileLock:
    return FileLock(str(checked_path(store(ws) / "history.lock")))


def operation_path(ws: Workspace, operation_id: str) -> Path:
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
        raise ValueError("operation_id must be a nonempty string of at most 200 characters.")
    key = hashlib.sha256(operation_id.encode()).hexdigest()
    return checked_path(store(ws) / "operations" / key / "operation.json")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_json(path: Path, value: Any) -> None:
    checked_path(path)
    write_json(path, value)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def checkpoint(req: Request, *, ledger_seq: int = 0) -> dict:
    return {"schema": 1, "kind": "message", "history_revision": req.history_revision,
            "through_message_id": req.chat[-1].id if req.chat else None,
            "message_count": len(req.chat), "full_chat": True, "ledger_seq": ledger_seq,
            "filesystem": "current", "provider_context": "fresh"}


def read_checkpoint(ws: Workspace) -> dict:
    value = read_json(checked_path(ws.memory / CHECKPOINT), {}) or {}
    if value and (value.get("schema") != 1 or value.get("kind") != "message"
                  or type(value.get("ledger_seq")) is not int or value["ledger_seq"] < 0):
        raise ValueError("Invalid conversation checkpoint; restore its recovery backup.")
    return value


def initialize_context(ws: Workspace, req: Request, *, ledger_seq: int = 0) -> None:
    """A fresh host must replay all public messages, including their references.

    The sequence fence prevents the old append-only ledger's later human events
    from being injected into the new conversation. No native thread survives.
    """
    durable_json(ws.memory / "lead_session.json", {
        "threads": {}, "key": None, "seq": ledger_seq,
        "turns": sum(t.role in {"job_lead", "assistant"} for t in req.chat),
        "history_revision": req.history_revision, "full_chat": True,
    })
    durable_json(ws.memory / CHECKPOINT, checkpoint(req, ledger_seq=ledger_seq))


def _ledger_seq(ws: Workspace) -> int:
    # Stream the ledger; never duplicate its payload or edit the signed chain.
    import json

    seq = 0
    path = checked_path(ws.ledger / "ledger.jsonl")
    if path.exists():
        with path.open() as stream:
            for line in stream:
                if line.strip():
                    seq = max(seq, int(json.loads(line)["seq"]))
    return seq


def _context_paths(ws: Workspace) -> list[str]:
    paths = []
    checked_path(ws.memory)
    if ws.memory.exists():
        paths.extend(f"memory/{p.name}" for p in ws.memory.iterdir() if p.name not in KEEP_MEMORY)
    # Ask answers and editor hints also enter the wake prompt; worker and Codex
    # stores can otherwise resurrect future transcripts. Move, never copy them.
    for relative in ("agent", "swarm", "asks", ".code/editor_context.json"):
        candidate = checked_path(ws.root / relative)
        if candidate.exists():
            paths.append(relative)
    for relative in paths:
        checked_path(ws.root / relative)
    return sorted(paths)


def prepare_reset(ws: Workspace, record_path: Path, record: dict, before: Request, after: Request) -> None:
    """Persist recovery material before touching any active context."""
    directory = record_path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable_json(directory / "request-before.json", before.model_dump(mode="json"))
    durable_json(directory / "request-after.json", after.model_dump(mode="json"))
    record.update(status="prepared", context_paths=_context_paths(ws), ledger_seq=_ledger_seq(ws))
    durable_json(record_path, record)


def finish_reset(ws: Workspace, record_path: Path, record: dict) -> dict:
    """Roll forward after a crash, without overwriting a newer conversation.

    Moved directories are their own backups, so native rollouts incur no second
    large copy. All backups remain outside the agent-readable work directory.
    """
    directory = record_path.parent
    before = read_json(directory / "request-before.json")
    after = read_json(directory / "request-after.json")
    current = ws.load().model_dump(mode="json")
    if current != before and current != after:
        raise HistoryConflict("The conversation changed during recovery; its backup has been retained.")
    for relative in record["context_paths"]:
        source = checked_path(ws.root / relative)
        saved = checked_path(directory / "saved" / relative)
        if saved.exists():
            continue
        if source.exists():
            saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(source, saved)
            sync_directory(saved.parent)
            sync_directory(source.parent)
    for name in ("memory", "asks", "swarm"):
        checked_path(ws.root / name).mkdir(exist_ok=True)
    req = Request.model_validate(after)
    initialize_context(ws, req, ledger_seq=record["ledger_seq"])
    # Publish history last: readers can never see truncated chat with a future
    # provider continuation still in its active location.
    durable_json(ws.request_path, after)
    record["status"] = "complete"
    durable_json(record_path, record)
    return record["result"]
