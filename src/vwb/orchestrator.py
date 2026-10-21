"""One chat turn, end to end (control plane): intake -> index -> job lead -> review.

The orchestrator owns what no agent may skip: the request record, the index before
planning, and the guarantee that every committed output reaches review with a verified
package (spec D14: the best attempt always goes to review).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from vwb.agents import job_lead
from vwb.canvas.store import CanvasStore
from vwb.index.build import build_index
from vwb.render import pipeline
from vwb.review import page
from vwb.util.jsonio import read_json
from vwb.util.log import console, log
from vwb.verify.verifier import verify
from vwb.workspace import Workspace, references
from vwb.workspace import attachments as chat_attachments

MEDIA_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm")
_PATH = re.compile(r"(/[^\s\"'<>|]+?\.(?:mp4|mov|m4v|mkv|webm))\b", re.I)


def find_media_paths(message: str) -> list[Path]:
    """Absolute media paths written in the chat message itself."""
    return [Path(m.group(1)) for m in _PATH.finditer(message) if Path(m.group(1)).is_file()]


def ensure_review(ws: Workspace) -> list[dict[str, Any]]:
    """Every head version is rendered and verified, even if the agent stopped early."""
    store = CanvasStore(ws.canvas)
    done = []
    for out in store.outputs():
        v = store.head(out)
        if not (ws.qc / out / f"v{v}" / "verification.json").exists():
            log.info("control plane: %s v%d has no review package yet; rendering and verifying", out, v)
            try:
                pipeline.render(ws, out, v, actor="control_plane")
                verify(ws, out, v)
            except Exception as exc:  # recorded; the page shows it as not submitted
                log.error("could not finish %s v%d: %s", out, v, exc)
        ver = read_json(ws.qc / out / f"v{v}" / "verification.json", {})
        done.append({"output": out, "version": v, "triage": ver.get("triage", "NOT SUBMITTED")})
    return done


def _turn_lock(ws: Workspace) -> FileLock:
    """One turn at a time per conversation: a message sent while the main agent is still answering is refused."""
    lock = FileLock(str(ws.root / "turn.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise SystemExit(f"{ws.id}: the main agent is still answering the last message; send this one when it "
                         "replies.") from None
    return lock


def chat(message: str, attach: list[Path] | None = None, request_id: str | None = None,
         skip_index: bool = False, context: dict[str, list[str]] | None = None,
         main_config: dict[str, Any] | None = None,
         attachment_refs: list[dict[str, Any]] | None = None,
         resume_message: str | None = None) -> dict[str, Any]:
    """One message of a request's conversation: without request_id it starts a new request, with it it continues."""
    attach = [Path(p).expanduser().resolve() for p in (attach or [])]
    if request_id:
        ws = Workspace.open(request_id)
        context = references.validate(context, ws)
        lock = _turn_lock(ws)
        first = len([t for t in ws.load().chat if t.role == "job_lead"]) == 0
    else:
        if attachment_refs:
            raise ValueError("Attachment references require their existing conversation.")
        context = references.validate(context)
        ws = Workspace.create(message, [])
        lock = _turn_lock(ws)
        first = True
        console.print(f"[bold]request[/] {ws.id} -> {ws.root}")
    try:
        refs = chat_attachments.validate_refs(ws, attachment_refs)
        refs += chat_attachments.ingest(ws, attach or ([] if request_id else find_media_paths(message)))
        if len(refs) > chat_attachments.MAX_FILES:
            raise ValueError("Attach up to 12 files per message.")
        req = ws.load()
        if not req.message and not req.chat:
            req.message = message
        for ref, path in chat_attachments.source_paths(ws, refs):
            existing = next((source for source in req.sources if source.sha256 == ref["sha256"]), None)
            if existing is None:
                req.sources.append(ws.add_source(path, f"src{len(req.sources) + 1}"))
        if request_id:
            from vwb.workspace.request import ChatTurn
            req.chat.append(ChatTurn(role="human", text=message, context=context, attachments=refs))
        else:
            req.chat[0].context = context
            req.chat[0].attachments = refs
        ws.save(req)
        if ws.load().sources and not skip_index:
            console.print("[bold]index[/] building the edit index (cached stages are skipped)")
            build_index(ws, progress=lambda m: console.print(f"  [dim]{m}[/]"))
        kwargs = {"cfg": main_config} if main_config else {}
        continuation = ("\n\n[harness] The user chose Resume turn after an interruption. Continue the saved thread and "
                        "inspect the workspace and tool records before acting. Completed tool operations already took effect; "
                        "do not repeat completed work or assume an interrupted operation completed. Earlier helpers may have "
                        "stopped; check their status. The original request below is context, not new permission:\n" + resume_message
                        if resume_message is not None else "")
        reply = job_lead.run(ws, message + references.prompt(context) + continuation, first_turn=first, **kwargs)
        outputs = ensure_review(ws)
        review = page.build(ws)
    finally:
        lock.release()
    return {"request": ws.id, "reply": reply, "outputs": outputs, "review_page": str(review)}
