"""A loopback-bound HTTP server with optional protected public HTTPS access.

Mutations require a per-server CSRF token and same-origin browser requests. Files are
served only from explicit media/review directories, never arbitrary local paths.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import threading
import uuid
import webbrowser
from contextlib import ExitStack, nullcontext
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from filelock import FileLock, Timeout

from vwb import models, prompts
from vwb.agents.events import context_usage, event_page
from vwb.artifacts import service as artifacts
from vwb.browser.service import BrowserConflict, BrowserSession
from vwb.config import RUNS_DIR
from vwb.integrations import IntegrationError, IntegrationService
from vwb.studio import assets as studio_assets
from vwb.studio.models import capabilities as studio_capabilities
from vwb.studio.service import StudioJobs
from vwb.util.jsonio import read_json, write_json
from vwb.web import pdf_preview, workspace_files
from vwb.web.history import request_ids, resolve_request
from vwb.web.queue import QueueConflict, TurnQueue
from vwb.workspace import Workspace, references
from vwb.workspace import attachments as chat_attachments
from vwb.workspace.checkpoints import HistoryConflict, read_checkpoint
from vwb.workspace.local_imports import LocalImports
from vwb.workspace.projects import ProjectConflict, ProjectStore
from vwb.workspace.request import SUBDIRS, Request, now_iso

STATIC_DIR = Path(__file__).parent / "static"
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
FILE_DIRS = {"media", "renders", "qc", "review", "release"}
FILE_TYPES = VIDEO_TYPES | {".jpg", ".jpeg", ".png", ".webp", ".gif", ".html", ".json", ".md", ".txt", ".srt", ".vtt", ".wav", ".mp3"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z")
MAX_BODY = 1024 * 1024
MAX_UPLOAD = 4 * 1024**3


def safe_child(root: Path, relative: str) -> Path:
    """Reject traversal, including symlinks that escape the allowed directory."""
    if not relative or "\x00" in relative or "\\" in relative:
        raise ValueError("Invalid file path.")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid file path.")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("The file is outside this workspace.")
    return resolved


def _rows(path: Path) -> list[dict[str, Any]]:
    """An append can still be in flight when a browser polls."""
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except ValueError:
            continue
    return rows


class Workbench:
    def __init__(self, runs_dir: Path = RUNS_DIR, *, queue: TurnQueue | None = None, public_url: str = ''):
        from vwb.web.public_access import PublicAccess

        self.public_access = PublicAccess(public_url) if public_url else None
        self.runs_dir = runs_dir.resolve()
        self.queue = queue or TurnQueue(self.runs_dir)
        self.csrf_token = secrets.token_urlsafe(32)
        self.qc_token = secrets.token_urlsafe(32)
        self.upload_dir = self.runs_dir / ".ui" / "uploads"
        self.mutation_lock = threading.RLock()
        self.studio = StudioJobs()
        self.local_imports = LocalImports()

    @property
    def projects(self) -> ProjectStore:
        return ProjectStore(self.runs_dir)

    def require_idle(self, ws: Workspace) -> None:
        state = self.queue.snapshot(ws)
        if state["active"] or state["queue"]:
            raise QueueConflict("Stop the active turn and remove queued messages before changing conversation history or project.")
        from vwb.web.qc import workspace as qc_workspace
        if (ws.root / ".qc-chat/runtime/request.json").exists():
            qc_state = self.queue.snapshot(qc_workspace(ws))
            if qc_state["active"] or qc_state["queue"]:
                raise QueueConflict("Stop QC and remove its queued messages before changing conversation history or project.")
        for jobs in (self.local_imports.list(ws)["jobs"], self.studio.list(ws)["jobs"]):
            if any(job["status"] in {"queued", "running", "preparing"} for job in jobs):
                raise QueueConflict("Wait for file imports and media generation to finish first.")

    def move_project(self, ws: Workspace, body: dict) -> dict:
        with self.mutation_lock, self.queue.lock:
            self.require_idle(ws)
            target = self.projects.folder(body.get("project_id"))
            for rid in request_ids(self.runs_dir):
                other = self.workspace(rid)
                if other.project_root in {target, ws.project_root} and other.project_root:
                    self.require_idle(other)
            return self.projects.move(ws, body.get("project_id"))

    def change_history(self, ws: Workspace, body: dict) -> dict:
        from vwb.workspace import conversations
        from vwb.workspace.checkpoints import checked_path, durable_json
        operation = body.get("operation_id")
        if not isinstance(operation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation):
            raise ValueError("History changes need a stable operation_id.")
        if type(body.get("expected_revision")) is not int or body["expected_revision"] < 0:
            raise ValueError("Reload the conversation before changing its history.")
        receipt = checked_path(ws.root / ".conversation-history/http" / f"{operation}.json")
        signature = json.dumps(body, sort_keys=True, separators=(",", ":"))
        with self.mutation_lock, self.queue.lock:
            saved = read_json(receipt)
            if saved and saved["signature"] != signature:
                raise HistoryConflict("That operation already belongs to a different history change.")
            if saved and saved.get("response"):
                return saved["response"]
            if saved and saved.get("history"):
                target = self.workspace(saved["history"]["request_id"])
                job = self.queue.submitted(target, f"history:{operation}")
                if job or saved["history"].get("edited_message") is None:
                    response = {"request_id": target.id, "history": saved["history"], "job": job}
                    saved["response"] = response
                    durable_json(receipt, saved)
                    return response
                if target.load().history_revision != saved["history"]["history_revision"]:
                    raise HistoryConflict("History changed after this edit was prepared. Its backup is retained; choose the current message again.")
            self.require_idle(ws)
            locks = [ws.root / "turn.lock"]
            if ws.project_root:
                locks.append(ws.project_root / "turn.lock")
            try:
                with ExitStack() as stack:
                    for path in sorted(locks):
                        stack.enter_context(FileLock(str(path), timeout=0))
                    main_config = models.explicit_runtime(ws, self.runs_dir)
                    if body.get("action") == "edit" and not saved:
                        selected = next((turn for turn in ws.load().chat if turn.id == body.get("message_id")), None)
                        if selected:
                            references.validate(selected.context, ws)
                            chat_attachments.validate_refs(ws, selected.attachments)
                    destination = self.runs_dir / ("r-" + uuid.uuid5(uuid.NAMESPACE_URL, f"{ws.id}:{operation}").hex[:16])
                    saved = saved or {"signature": signature, "stage": "prepared"}
                    durable_json(receipt, saved)
                    changed = conversations.apply(ws, destination, body)
                    target, result = changed["workspace"], changed["result"]
                    if saved["stage"] == "prepared":
                        if target.id == ws.id:
                            self.queue.reset(ws, turn_lock_held=True)
                        saved["stage"] = "reset"
                        saved["history"] = result
                        durable_json(receipt, saved)
                    job = None
                    edited = result.get("edited_message")
                    if edited is not None:
                        job = self.queue.enqueue(target, message=edited["text"], mode="now",
                            attachment_refs=edited["attachments"], context=edited["context"],
                            skip_index=not any(ref["kind"] == "video" for ref in edited["attachments"]),
                            main_config=main_config, idempotency_key=f"history:{operation}", turn_lock_held=target.id == ws.id)
                    response = {"request_id": target.id, "history": result, "job": job}
                    saved["response"] = response
                    durable_json(receipt, saved)
                    return response
            except Timeout:
                raise QueueConflict("Stop the active conversation or project turn before changing its history.") from None

    def studio_entry(self, ws: Workspace, row: dict) -> dict:
        """Public studio records use the same capability-scoped file previews."""
        row = dict(row)
        if row.get("path"):
            row["url"] = self.preview_entry(ws.id, {"kind": "file", "path": row["path"]})["url"]
        if row.get("result"):
            row["result"] = dict(row["result"])
            row["result"]["artifacts"] = [self.studio_entry(ws, item) for item in row["result"].get("artifacts", [])]
            if row["result"].get("background_mask"):
                row["result"]["background_mask"] = self.studio_entry(ws, row["result"]["background_mask"])
        if row.get("reusable_inputs"):
            row["reusable_inputs"] = [self.studio_entry(ws, item) for item in row["reusable_inputs"]]
        if row.get("reusable_mask"):
            row["reusable_mask"] = self.studio_entry(ws, row["reusable_mask"])
        return row

    def workspace(self, request_id: str) -> Workspace:
        return Workspace(resolve_request(self.runs_dir, request_id))

    @property
    def integrations(self) -> IntegrationService:
        return IntegrationService(runs_dir=self.runs_dir)

    def preview_key(self, request_id: str) -> str:
        # Opaque HTML previews have Origin:null. A request-scoped capability
        # permits their local assets without admitting arbitrary cross-site reads.
        return hmac.new(self.csrf_token.encode(), f"preview:{request_id}".encode(), "sha256").hexdigest()

    def artifact_key(self, request_id: str, artifact_id: str, revision: int) -> str:
        return hmac.new(self.csrf_token.encode(), f"artifact:{request_id}:{artifact_id}:{revision}".encode(), "sha256").hexdigest()

    def artifact_entry(self, ws: Workspace, row: dict) -> dict:
        key = self.artifact_key(ws.id, row["id"], row["revision"])
        return {**artifacts.public(row), "url": f"/artifact-preview/{quote(ws.id)}/{key}/{row['id']}/{row['revision']}/index.html"}

    def preview_entry(self, request_id: str, entry: dict) -> dict:
        if entry.get("kind") != "file":
            return entry
        return {**entry, "url": f"/preview/{quote(request_id)}/{self.preview_key(request_id)}/{quote(entry['path'], safe='/')}"}

    def close_browsers(self) -> None:
        """Stop UI-managed daemons after workers stop; preserve private profiles.

        Browser sessions started from an independent CLI are not adopted merely
        by reading their status. Remote Chrome belongs to its configured host.
        """
        for request_id in request_ids(self.runs_dir):
            try:
                ws = self.workspace(request_id)
                state_path = ws.root / ".browser" / "state.json"
                if (not state_path.is_symlink() and state_path.is_file()
                        and read_json(state_path, {}).get("ui_managed")):
                    BrowserSession(ws).ui_action({"action": "close"})
            except Exception:
                # Other sessions still need cleanup if one external endpoint is
                # unavailable. Keep its durable state for the next explicit close.
                continue

    @staticmethod
    def asks(ws: Workspace) -> list[dict[str, Any]]:
        # Broker.tickets() applies deadlines. Merely opening an old conversation
        # must not mutate its answers or ledger.
        return sorted([read_json(p) for p in (ws.asks / "tickets").glob("*.json")],
                      key=lambda t: t.get("created", ""))

    def requests(self) -> list[dict[str, Any]]:
        requests = []
        for request_id in request_ids(self.runs_dir):
            try:
                ws = self.workspace(request_id)
                req = ws.load()
                state = self.queue.snapshot(ws)
                output_count = len(list(ws.canvas.glob("*/HEAD")))
                title = (req.spec.summary if req.spec else "") or req.message
                requests.append({"id": ws.id, "title": " ".join(title.split())[:140], "created": req.created,
                                 "project_id": ws.project_root.name if ws.project_root else None,
                                 "updated": req.chat[-1].ts if req.chat else req.created,
                                 "status": "working" if state["active"] else ("in_review" if output_count else "open"),
                                 "source_count": len(req.sources), "output_count": output_count,
                                 "open_questions": sum(t["status"] in {"open", "defaulted"} for t in self.asks(ws)),
                                 "active": bool(state["active"]), "queued": len(state["queue"])})
            except (ValueError, KeyError, OSError):
                continue
        return sorted(requests, key=lambda r: r["updated"], reverse=True)

    @staticmethod
    def file_url(ws: Workspace, path: Path) -> str | None:
        try:
            relative = path.resolve().relative_to(ws.root.resolve())
        except ValueError:
            return None
        if not path.is_file() or relative.parts[0] not in FILE_DIRS:
            return None
        return f"/files/{quote(ws.id)}/{quote(relative.as_posix())}"

    def outputs(self, ws: Workspace, *, qc_store=None, qc_outputs: set[str] | None = None) -> list[dict[str, Any]]:
        flags: dict[str, dict[str, Any]] = {}
        for ev in _rows(ws.ledger / "ledger.jsonl"):
            data = ev.get("data", {})
            if ev.get("kind") == "flag.raised":
                flags[data.get("id", "")] = data
            elif ev.get("kind") == "flag.cleared":
                flags.pop(data.get("id", ""), None)
            elif ev.get("kind") in {"human.ack", "human.waive"}:
                flags.pop(data.get("id", ""), None)
        rows = []
        for head in sorted(ws.canvas.glob("*/HEAD")):
            out = head.parent.name
            if not IDENTIFIER.fullmatch(out):
                continue
            try:
                version = int(head.read_text().strip())
            except ValueError:
                continue
            canvas = read_json(head.parent / f"v{version}.json", {})
            qc = ws.qc / out / f"v{version}"
            verification = read_json(qc / "verification.json", {})
            render = ws.renders / out / f"v{version}"
            receipt = read_json(render / "receipt.json", {})
            fmt = canvas.get("format", {})
            review_path = ws.review / "index.html"
            rows.append({"id": out, "output": out, "version": version,
                         "triage": verification.get("triage", "NOT SUBMITTED"),
                         "video_url": self.file_url(ws, render / f"{out}_v{version}.mp4"),
                         "thumbnail_url": self.file_url(ws, qc / "thumbnail.jpg"),
                         "width": fmt.get("width"), "height": fmt.get("height"),
                         "duration_s": receipt.get("seconds"),
                         "flags": [f for f in flags.values() if f.get("output") == out and f.get("version") == version],
                         "review_url": self.file_url(ws, review_path)})
            if qc_store is not None and (qc_outputs is None or out in qc_outputs) and rows[-1]["video_url"]:
                from vwb.qc.chat_store import QCStoreError
                try:
                    target = qc_store.describe_target(f"output:{out}@v{version}")
                    # A receipt may point at another file. Only certify the exact
                    # video displayed by this output row, never a nearby render.
                    displayed = (render / f"{out}_v{version}.mp4").relative_to(ws.root).as_posix()
                    if target["file"] == displayed:
                        rows[-1]["qc_target"] = {
                            key: target[key] for key in ("target", "output", "version", "file", "fps", "frames",
                                                        "sha256", "canvas_file_sha256")
                        }
                        rows[-1]["qc_target"]["url"] = rows[-1]["video_url"]
                except (QCStoreError, OSError):
                    # The sent finding remains visible, but cannot seek into an
                    # unverified render. Do not expose private store diagnostics.
                    pass
        return rows

    def snapshot(self, ws: Workspace, *, after: int | None = None, before: int | None = None,
                 limit: int = 300, full: bool = True, include_events: bool = True,
                 history_epoch: int | None = None) -> dict[str, Any]:
        if history_epoch is not None and (type(history_epoch) is not int or history_epoch < 0):
            raise ValueError("Choose a nonnegative history epoch.")
        # Keep the epoch, event file and public chat on the same side of a reset.
        with self.mutation_lock:
            epoch = read_checkpoint(ws).get("history_revision", 0)
            reset = history_epoch is not None and history_epoch != epoch
            payload = {"history_epoch": epoch, "asks": self.asks(ws), "usage": context_usage(ws),
                       **self.queue.snapshot(ws)}
            if include_events or reset:
                payload.update(event_page(ws, after=None if reset else after,
                                          before=None if reset else before, limit=limit))
            if full or reset:
                req = ws.load().model_dump(mode="json")
                for source in req["sources"]:
                    source["name"] = Path(source["original_path"]).name
                    source["url"] = self.file_url(ws, Path(source["path"]))
                    source.pop("original_path", None)
                    source.pop("path", None)
                for turn in req["chat"]:
                    turn["attachments"] = [self.attachment_entry(ws, ref) for ref in turn.get("attachments", [])]
                from vwb.qc.chat_store import QCStore, QCStoreError
                qc_store, qc_findings = None, []
                # Reading Edit must not initialize QC, reconcile findings, or
                # consult live/private item state. Only sent snapshots cross over.
                if (ws.root / ".qc-chat/events.jsonl").is_file():
                    try:
                        qc_store = QCStore(ws)
                        qc_findings = qc_store.list_items(audience="editor")
                    except (QCStoreError, OSError):
                        qc_store = None
                payload.update(request=req, outputs=self.outputs(ws, qc_store=qc_store if qc_findings else None,
                               qc_outputs={item["anchor"]["output"] for item in qc_findings}),
                               qc_findings=qc_findings)
            for job in [*payload.get("queue", []), *([payload["active"]] if payload.get("active") else [])]:
                job["attachment_refs"] = [self.attachment_entry(ws, ref) for ref in job.get("attachment_refs", [])]
            return payload

    @staticmethod
    def attachment_entry(ws: Workspace, ref: dict) -> dict:
        return {**ref, "url": f"/api/requests/{quote(ws.id)}/attachments/{ref['id']}",
                "workspace_path": f"work/{ref['path']}"}

    def attachment_paths(self, ids: Any) -> list[Path]:
        if not isinstance(ids, list) or len(ids) > chat_attachments.MAX_FILES:
            raise ValueError(f"Attach up to {chat_attachments.MAX_FILES} files per message, or use @ to reference "
                             "a workspace folder or import a local folder for a larger batch.")
        paths = []
        for upload_id in ids:
            if not isinstance(upload_id, str) or not re.fullmatch(r"[a-f0-9]{32}", upload_id):
                raise ValueError("Invalid upload id.")
            metadata = read_json(self.upload_dir / f"{upload_id}.json")
            if not metadata:
                raise ValueError("An attachment has expired. Attach it again.")
            path = safe_child(self.upload_dir, metadata["file"])
            if not path.is_file() or path.is_symlink():
                raise ValueError("An attachment is missing. Attach it again.")
            if metadata.get("sha256"):
                from vwb.util.hashing import sha256_file
                if sha256_file(path) != metadata["sha256"]:
                    raise ValueError("The upload changed after it was attached. Attach it again.")
            paths.append(path)
        return paths

    @staticmethod
    def message(body: dict[str, Any]) -> str:
        text = body.get("message", "")
        if not isinstance(text, str) or (not text.strip() and not body.get("attachments")):
            raise ValueError("Write a message or attach a file first.")
        if len(text) > 100_000:
            raise ValueError("The message is too long (maximum 100,000 characters).")
        return text.strip()

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        message = self.message(body)
        context = references.validate(body.get("context"))
        main_config = models.explicit_runtime(runs_dir=self.runs_dir)
        paths = self.attachment_paths(body.get("attachments", []))
        rid = f"r-{uuid.uuid4().hex[:16]}"
        ws = Workspace(self.runs_dir / rid)
        ws.root.mkdir()
        try:
            for sub in SUBDIRS:
                (ws.root / sub).mkdir()
            ws.save(Request(id=rid, message=message))
            if body.get("project_id") is not None:
                self.projects.move(ws, body["project_id"])
            refs = chat_attachments.ingest(ws, paths)
            sources = [ws.add_source(path, f"src{i}") for i, (_, path) in enumerate(chat_attachments.source_paths(ws, refs), 1)]
            # The worker adds the first chat message under the turn lock.
            ws.update(sources=sources)
            if main_config:
                models.set_selection(ws, main_config["selection"], self.runs_dir)
            prompts.set_selection(ws, prompts.selection(runs_dir=self.runs_dir))
            job = self.queue.enqueue(ws, message=message, attachment_refs=refs, skip_index=not sources, context=context, main_config=main_config)
        except BaseException:
            shutil.rmtree(ws.root)
            raise
        return {"request": {"id": rid}, "request_id": rid, "job": job}

    def draft_workspace(self, body: dict | None = None) -> dict[str, Any]:
        """Create a place for imported files without submitting a model turn."""
        rid = f"r-{uuid.uuid4().hex[:16]}"
        ws = Workspace(self.runs_dir / rid)
        ws.root.mkdir()
        try:
            for sub in SUBDIRS:
                (ws.root / sub).mkdir()
            ws.save(Request(id=rid, message=""))
            if body and body.get("project_id") is not None:
                self.projects.move(ws, body["project_id"])
            prompts.set_selection(ws, prompts.selection(runs_dir=self.runs_dir))
        except BaseException:
            shutil.rmtree(ws.root)
            raise
        return {"request": {"id": rid}, "request_id": rid}

    def answer(self, ws: Workspace, body: dict[str, Any]) -> dict[str, Any]:
        from vwb.ask.broker import Broker
        tid = body.get("ticket", "")
        if not isinstance(tid, str) or not IDENTIFIER.fullmatch(tid):
            raise ValueError("Invalid question id.")
        option, text, mode = body.get("option"), body.get("text", ""), body.get("mode", "queue")
        if not isinstance(text, str) or len(text) > 100_000 or (option is not None and not isinstance(option, str)):
            raise ValueError("Invalid answer.")
        if mode not in {"queue", "now"}:
            raise ValueError("Unknown message mode.")
        if not option and not text.strip():
            raise ValueError("Choose an option or write your answer.")
        broker = Broker(ws)
        with self.mutation_lock, self.queue.lock, broker.lock:
            ticket = read_json(ws.asks / "tickets" / f"{tid}.json")
            if not ticket:
                raise FileNotFoundError("That question was not found.")
            if ticket.get("status") == "answered":
                raise QueueConflict("That question has already been answered. Send a new message to change your direction.")
            if self.queue.closed.is_set():
                raise QueueConflict("The UI server is shutting down.")
            # Verify a steer can be accepted before signing the answer.
            active = self.queue.snapshot(ws)["active"]
            # A live wait lease is authoritative even before queue activity is
            # visible (or for a helper/terminal waiter). Queueing here duplicates
            # the continuation after the tool has already consumed the answer.
            waiting = bool(broker.active_waiters(ticket))
            if mode == "now" and active and active.get("kind") == "external" and not waiting:
                raise QueueConflict("This turn was started outside the UI. Queue the answer, or stop it in its terminal.")
            # Check the selected provider before signing a human answer. Queue jobs
            # retain these settings even if the person changes the menu afterward.
            main_config = None if waiting else models.explicit_runtime(ws, self.runs_dir)
            label = next((str(o.get("label") or o.get("text") or o["id"]) for o in ticket.get("options", []) if o["id"] == option), option or "")
            message = f"Answer to: {ticket['question']}\n{label}"
            if text.strip():
                message += f"\n{text.strip()}"
            self.message({"message": message})
            answered = broker.answer(tid, option, text=text.strip(), by="local-ui",
                                     delivery="waiting_tool" if waiting else None)
            if waiting:
                # The blocked tool consumes this signed answer in its existing turn.
                # Preserve it in the ticket/ledger; do not race request.json writers.
                return {"ticket": answered, "job": None, "delivery": "waiting_tool"}
            job = self.queue.enqueue(ws, message=message.strip(), mode=mode, main_config=main_config)
            return {"ticket": answered, "job": job,
                    "delivery": "interrupt_then_resume" if mode == "now" and active else "queued"}

    def compact(self, ws: Workspace) -> dict[str, Any]:
        with self.queue.lock:
            state = self.queue.snapshot(ws)
            if state["active"] or state["queue"]:
                raise QueueConflict("Finish or stop the current turn and send or remove queued messages before compacting.")
            if not context_usage(ws)["compact_available"]:
                raise QueueConflict("Compaction is available after this conversation has a saved Codex thread.")
            # Compaction is an explicit action and must also start after a stopped
            # turn left an otherwise empty queue paused.
            return self.queue.enqueue(ws, kind="compact", mode="now")

    def file(self, request_id: str, relative: str) -> Path:
        ws = self.workspace(request_id)
        path = safe_child(ws.root, relative)
        if Path(relative).parts[0] not in FILE_DIRS or path.suffix.lower() not in FILE_TYPES:
            raise ValueError("This file is not available in the browser.")
        # A symlink into memory/ or agent/ is not an allowed artifact either.
        if path.relative_to(ws.root.resolve()).parts[0] not in FILE_DIRS:
            raise ValueError("This file is not available in the browser.")
        if not path.is_file():
            raise FileNotFoundError("The file was not found.")
        return path


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workbench: Workbench):
        if address[0] != "127.0.0.1":
            raise ValueError("The workbench server must bind to 127.0.0.1.")
        self.workbench = workbench
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: WorkbenchServer
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            # A browser can close a keepalive socket between requests, before
            # our per-request handler has been entered.
            self.close_connection = True

    def log_message(self, format, *args):
        pass  # polling should not flood the terminal

    @property
    def app(self) -> Workbench:
        return self.server.workbench

    def _json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _check_host(self) -> str:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.app.public_access:
            hosts.add(self.app.public_access.host)
        host = self.headers.get("Host", "")
        if len(self.headers.get_all('Host', [])) != 1 or host not in hosts:
            raise PermissionError("Open the workbench at its configured address.")
        return host

    def _request_origin(self) -> str:
        host = self._check_host()
        access = self.app.public_access
        return access.origin if access and host == access.host else f'http://{host}'

    def _check_origin(self, mutate: bool = False) -> None:
        expected = self._request_origin()
        origin = self.headers.get("Origin")
        if len(self.headers.get_all('Origin', [])) > 1 or origin and origin != expected:
            raise PermissionError("Cross-origin requests are not allowed.")
        public_navigation = (self.app.public_access and self.command in {'GET', 'HEAD'}
                             and urlsplit(self.path).path == '/'
                             and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                             and self.headers.get('Sec-Fetch-Dest') == 'document' and not origin)
        if self.headers.get("Sec-Fetch-Site") == "cross-site" and not public_navigation:
            raise PermissionError("Cross-site requests are not allowed.")
        if mutate and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.app.csrf_token):
            raise PermissionError("Reload the workbench before making changes.")

    def _check_qc_session(self) -> None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(f"vwb_qc_{self.server.server_port}")
            token = value.value if value else ""
        except Exception:
            token = ""
        if not hmac.compare_digest(token, self.app.qc_token):
            raise PermissionError("Open the Workbench launch link printed by vwb ui to access private QC.")

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked request bodies are not supported.")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= MAX_BODY:
            raise ValueError("The request is too large.")
        raw = self.rfile.read(length)
        body = json.loads(raw) if raw else {}
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        return body

    def _route(self) -> None:
        url = urlsplit(self.path)
        parts = unquote(url.path).strip("/").split("/")
        query = parse_qs(url.query)
        if self.app.public_access and self.app.public_access.gate(self, url, parts):
            return
        if self.command == "GET" and parts == [""] and "qc_access" in query:
            self._check_origin()
            if not hmac.compare_digest(query["qc_access"][0], self.app.qc_token):
                raise PermissionError("This Workbench launch link expired. Use the current vwb ui link.")
            self.send_response(303)
            secure = '; Secure' if self._request_origin().startswith('https://') else ''
            self.send_header("Set-Cookie", f"vwb_qc_{self.server.server_port}={self.app.qc_token}; HttpOnly; SameSite=Strict; Path=/api/requests/{secure}")
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.command == "GET" and parts == ["api", "integrations", "solstice", "callback"]:
            self._check_host()
            return self._integration_callback(query)
        if self.command == "GET" and parts == ["api", "integrations", "figma-direct", "callback"]:
            self._check_host()
            return self._integration_callback(query, direct=True)
        if self.command == "GET" and parts == ["api", "integrations", "mcp", "callback"]:
            self._check_host()
            return self._integration_callback(query, remote=True)
        if self.command in {"GET", "HEAD"} and len(parts) >= 4 and parts[0] == "preview":
            self._check_host()
            if not hmac.compare_digest(parts[2], self.app.preview_key(parts[1])):
                raise PermissionError("Reopen this file from the workspace to refresh its preview link.")
            # Opaque previews send Origin:null for CORS loads, but no Origin at
            # all for classic scripts, images and styles. The capability is the
            # authority for both; explicit foreign origins remain disallowed.
            if self.headers.get("Origin") not in {None, "null"}:
                self._check_origin()
            ws = self.app.workspace(parts[1])
            relative = "/".join(parts[3:])
            with workspace_files.open_file(ws, relative) as handle:
                return self._file(Path(relative), preview=True, handle=handle)
        if self.command in {"GET", "HEAD"} and len(parts) >= 6 and parts[0] == "artifact-preview":
            self._check_host()
            ws = self.app.workspace(parts[1])
            row = artifacts.get(ws, parts[3], int(parts[4]))
            if not hmac.compare_digest(parts[2], self.app.artifact_key(ws.id, row["id"], row["revision"])):
                raise PermissionError("Reopen this artifact to refresh its preview link.")
            if self.headers.get("Origin") not in {None, "null"}:
                self._check_origin()
            base = f"/artifact-preview/{quote(ws.id)}/{parts[2]}/{row['id']}/{row['revision']}"
            if len(parts) == 6 and parts[5] == "index.html":
                urls = {item["path"]: f"{base}/asset/{i}" for i, item in enumerate(row["assets"])}
                return self._artifact_html(artifacts.render_html(ws, row, urls))
            if len(parts) == 7 and parts[5] == "asset" and parts[6].isdigit():
                index = int(parts[6])
                if index >= len(row["assets"]):
                    raise FileNotFoundError("This artifact asset was not found.")
                item = row["assets"][index]
                with workspace_files.open_file(ws, item["path"]) as handle:
                    artifacts.check_asset(handle, item)
                    return self._file(Path(item["path"]), preview=True, handle=handle)
            raise FileNotFoundError("This artifact resource was not found.")
        self._check_origin(self.command not in {"GET", "HEAD"})
        if len(parts) >= 4 and parts[:2] == ["api", "requests"] and parts[3] == "qc":
            self._check_qc_session()
        if self.command in {"GET", "HEAD"}:
            if parts == ["api", "canvas-renderers"]:
                from vwb.canvas.renderers import catalog as renderer_catalog
                from vwb.desktop.after_effects import AfterEffects
                return self._json(renderer_catalog(after_effects_status=AfterEffects(self.app.runs_dir).status()))
            if parts == ["api", "bootstrap"]:
                return self._json({"csrf_token": self.app.csrf_token, "capabilities": {
                    "upload": True, "queue": True, "steer": "interrupt_then_resume", "compact": "native",
                    "max_upload_bytes": MAX_UPLOAD, "max_attachments": chat_attachments.MAX_FILES,
                    "attachment_types": "any", "image_attachments": True,
                    "path_references": True, "local_imports": True, "max_references": references.MAX_REFERENCES}})
            if len(parts) == 3 and parts[:2] == ["api", "uploads"]:
                path = self.app.attachment_paths([parts[2]])[0]
                row = read_json(self.app.upload_dir / f"{parts[2]}.json")
                return self._file(path, artifact=True, download=row.get("kind") not in {"image", "video", "audio", "pdf"})
            if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3] == "attachments":
                ws = self.app.workspace(parts[2])
                row = chat_attachments.canonical(ws, parts[4])
                path = chat_attachments.original_path(ws, row)
                with path.open("rb") as handle:
                    return self._file(Path(row["name"]), artifact=True, handle=handle,
                                      download=row["kind"] not in {"image", "video", "audio", "pdf"})
            if parts == ["api", "requests"]:
                return self._json({"requests": self.app.requests(), "projects": self.app.projects.list()})
            if parts == ["api", "projects"]:
                return self._json({"projects": self.app.projects.list()})
            if len(parts) >= 4 and parts[:2] == ["api", "requests"] and parts[3] == "qc":
                from vwb.web import qc as qc_chat
                ws = self.app.workspace(parts[2])
                if len(parts) == 4:
                    return self._json(qc_chat.snapshot(self.app, ws))
                if len(parts) == 6 and parts[4] == "attachments":
                    qws = qc_chat.workspace(ws)
                    row = chat_attachments.canonical(qws, parts[5])
                    path = chat_attachments.original_path(qws, row)
                    with path.open("rb") as handle:
                        return self._file(Path(row["name"]), artifact=True, handle=handle,
                                          download=row["kind"] not in {"image", "video", "audio", "pdf"})
                raise FileNotFoundError("That QC endpoint was not found.")
            if parts == ["api", "features"]:
                return self._json(references.catalog(query=query.get("q", [""])[0]))
            if len(parts) in {4, 5} and parts[:2] == ["api", "requests"] and parts[3] == "imports":
                ws = self.app.workspace(parts[2])
                if len(parts) == 5:
                    return self._json({"import": self.app.local_imports.get(ws, parts[4])})
                return self._json(self.app.local_imports.list(ws))
            if parts == ["api", "models"]:
                return self._json(models.catalog(self.app.runs_dir))
            if parts == ["api", "prompts"]:
                return self._json(prompts.catalog(self.app.runs_dir))
            if parts == ["api", "integrations"]:
                return self._json(self.app.integrations.status())
            if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3:] == ["code", "context"]:
                from vwb.code.editor import read_context
                return self._json(read_context(self.app.workspace(parts[2])))
            if len(parts) in {5, 6} and parts[:2] == ["api", "requests"] and parts[3] == "studio":
                ws = self.app.workspace(parts[2])
                if parts[4] == "capabilities" and len(parts) == 5:
                    return self._json(studio_capabilities())
                if parts[4] == "video-models" and len(parts) == 5:
                    from vwb.studio.openrouter import video_models
                    return self._json(video_models(ws, query.get("query", [""])[0],
                                                   int(query.get("limit", [50])[0]), int(query.get("offset", [0])[0])))
                if parts[4] == "jobs":
                    if len(parts) == 6:
                        return self._json(self.app.studio_entry(ws, self.app.studio.get(ws, parts[5])))
                    result = self.app.studio.list(ws, query.get("kind", [None])[0])
                    result["jobs"] = [self.app.studio_entry(ws, row) for row in result["jobs"]]
                    return self._json(result)
            if len(parts) in {4, 5, 6} and parts[:2] == ["api", "requests"] and parts[3] == "artifacts":
                ws = self.app.workspace(parts[2])
                if len(parts) == 4:
                    return self._json({"artifacts": [self.app.artifact_entry(ws, row) for row in artifacts.listing(ws)]})
                if len(parts) == 5:
                    revision = query.get("revision", [None])[0]
                    return self._json(self.app.artifact_entry(ws, artifacts.get(ws, parts[4], int(revision) if revision else None)))
                if parts[5] == "feedback":
                    return self._json(artifacts.feedback(ws, parts[4], after=int(query.get("after", [0])[0])))
            if len(parts) in {4, 5} and parts[:2] == ["api", "requests"] and parts[3] == "workspace":
                ws = self.app.workspace(parts[2])
                relative = query.get("path", [""])[0]
                if len(parts) == 4:
                    listing = workspace_files.listing(ws, relative, cursor=query.get("cursor", [None])[0])
                    listing["entries"] = [self.app.preview_entry(ws.id, item) for item in listing["entries"]]
                    return self._json(listing)
                if parts[4] == "file":
                    return self._json(self.app.preview_entry(ws.id, workspace_files.metadata(ws, relative)))
                if parts[4] == "search":
                    result = workspace_files.search_files(ws, query.get("q", [""])[0], limit=40)
                    result["entries"] = [self.app.preview_entry(ws.id, item) for item in result["entries"]]
                    return self._json(result)
                if parts[4] == "pdf":
                    return self._json(pdf_preview.render(ws, relative, int(query.get("page", ["1"])[0])))
            if len(parts) in {4, 5} and parts[:2] == ["api", "requests"] and parts[3] == "browser":
                session = BrowserSession(self.app.workspace(parts[2]))
                if len(parts) == 4:
                    return self._json(session.status())
                if parts[4] == "stream":
                    return self._browser_stream(session)
            if len(parts) in {3, 4} and parts[:2] == ["api", "requests"]:
                ws = self.app.workspace(parts[2])
                epoch = int(query["history_epoch"][0]) if "history_epoch" in query else None
                if len(parts) == 3:
                    return self._json(self.app.snapshot(ws, include_events=query.get("events", ["1"])[0] != "0",
                                                        history_epoch=epoch))
                if parts[3] == "events":
                    if "before" in query:
                        if "after" in query:
                            raise ValueError("Choose either newer or older activity.")
                        return self._json(self.app.snapshot(ws, before=int(query["before"][0]), full=False,
                                                           limit=int(query.get("limit", ["300"])[0]),
                                                           history_epoch=epoch))
                    after = max(0, int(parse_qs(url.query).get("after", ["0"])[0]))
                    return self._json(self.app.snapshot(ws, after=after, full=False, history_epoch=epoch))
                if parts[3] == "features":
                    return self._json(references.catalog(ws, query=query.get("q", [""])[0]))
                if parts[3] == "model":
                    selected = models.selection(ws, self.app.runs_dir)
                    return self._json({"selection": selected, "model": models.describe(selected, self.app.runs_dir),
                                       "active_selection": (self.app.queue.snapshot(ws).get("active") or {}).get("main_config", {}).get("selection")})
                if parts[3] == "prompt":
                    return self._json(prompts.status(ws, self.app.queue.snapshot(ws).get("active"), self.app.runs_dir))
            if len(parts) >= 3 and parts[0] == "files":
                return self._file(self.app.file(parts[1], "/".join(parts[2:])), artifact=True)
            if parts[0] != "api":
                relative = unquote(url.path).lstrip("/") or "index.html"
                return self._file(safe_child(STATIC_DIR, relative))
        elif self.command == "PUT":
            if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3:] == ["code", "context"]:
                from vwb.code.editor import save_context
                return self._json(save_context(self.app.workspace(parts[2]), self._body()))
        elif self.command == "POST":
            if parts == ["api", "uploads"]:
                return self._upload()
            if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3:] == ["studio", "uploads"]:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Chunked uploads are not supported.")
                ws = self.app.workspace(parts[2])
                with self.app.mutation_lock:
                    result = studio_assets.upload(ws, self.rfile, int(self.headers.get("Content-Length", "0")),
                                                  name=unquote(self.headers.get("X-Filename", "input.png")),
                                                  kind=self.headers.get("X-Upload-Kind", "image"))
                    return self._json(self.app.studio_entry(ws, result), 201)
            body = self._body()
            if len(parts) >= 5 and parts[:2] == ["api", "requests"] and parts[3] == "qc":
                from vwb.web import qc as qc_chat
                return self._json(qc_chat.mutate(self.app, self.app.workspace(parts[2]), parts[4:], body))
            if len(parts) == 6 and parts[:2] == ["api", "requests"] and parts[3] == "artifacts" and parts[5] == "feedback":
                ws = self.app.workspace(parts[2])
                def enqueue_feedback(message, mode):
                    return self.app.queue.enqueue(ws, message=message, mode=mode, skip_index=True,
                                                  idempotency_key=f"artifact:{parts[4]}:{body.get('submission_id', '')}",
                                                  main_config=models.explicit_runtime(ws, self.app.runs_dir))
                return self._json({"feedback": artifacts.submit_feedback(ws, parts[4], body, enqueue_feedback)}, 202)
            if parts == ["api", "integrations", "mcp", "add"]:
                return self._json({"item": self.app.integrations.remote.add(body)}, 201)
            if len(parts) == 5 and parts[:3] == ["api", "integrations", "mcp"]:
                remote = self.app.integrations.remote
                identifier, action = parts[3:]
                if action == "connect":
                    return self._json(remote.connect(identifier, self._callback_uri(remote=True)))
                if action == "configure":
                    return self._json({"item": remote.configure(identifier, body)})
                if action in {"refresh", "disconnect", "remove"}:
                    return self._json({"item": getattr(remote, action)(identifier)})
            if len(parts) == 4 and parts[:3] == ["api", "integrations", "after-effects"]:
                service = self.app.integrations
                if parts[3] == "configure":
                    service.after_effects.configure(body)
                    return self._json(service.status())
                if parts[3] == "refresh":
                    return self._json(service.status())
            if len(parts) == 4 and parts[:3] == ["api", "integrations", "blender-mcp"]:
                service = self.app.integrations
                if parts[3] == "configure":
                    if set(body) - {"allow_code_execution", "port"}:
                        raise ValueError("Blender settings only accept Python execution permission and the local add-on port.")
                    service.blender.configure(**body)
                    return self._json(service.status())
                if parts[3] in {"prepare", "connect", "refresh", "disconnect"}:
                    getattr(service.blender, parts[3])()
                    return self._json(service.status())
            if len(parts) == 4 and parts[:3] == ["api", "integrations", "figma-mcp"]:
                desktop = self.app.integrations.desktop
                if parts[3] == "connect":
                    return self._json(desktop.connect())
                if parts[3] == "refresh":
                    return self._json(desktop.refresh())
                if parts[3] == "disconnect":
                    return self._json(desktop.disconnect())
            if len(parts) == 4 and parts[:3] == ["api", "integrations", "figma-direct"]:
                direct = self.app.integrations.direct
                if parts[3] == "configure":
                    return self._json(direct.configure(body))
                if parts[3] == "connect":
                    return self._json(direct.begin_auth(self._callback_uri(direct=True)))
                if parts[3] == "refresh":
                    return self._json(direct.verify())
                if parts[3] == "disconnect":
                    return self._json(direct.disconnect())
            if len(parts) in {5, 7} and parts[:2] == ["api", "requests"] and parts[3:5] == ["studio", "jobs"]:
                ws = self.app.workspace(parts[2])
                if len(parts) == 5:
                    with self.app.mutation_lock:
                        return self._json(self.app.studio_entry(ws, self.app.studio.submit(ws, body)), 202)
                if parts[6] == "cancel":
                    return self._json(self.app.studio_entry(ws, self.app.studio.cancel(ws, parts[5])))
            if parts == ["api", "integrations", "figma", "connect"]:
                return self._json(self.app.integrations.begin_figma_auth())
            if len(parts) == 3 and parts[:2] == ["api", "integrations"]:
                service = self.app.integrations
                if parts[2] == "configure":
                    return self._json(service.configure(body))
                if parts[2] == "connect":
                    return self._json(service.begin_auth(self._callback_uri()))
                if parts[2] == "refresh":
                    return self._json(service.refresh_catalog())
                if parts[2] == "disconnect":
                    return self._json(service.disconnect())
            if parts == ["api", "models", "preferences"]:
                return self._json(models.preferences(body, self.app.runs_dir))
            if parts == ["api", "prompts", "preferences"]:
                return self._json(prompts.preferences(body, self.app.runs_dir))
            if parts == ["api", "models", "resolve"]:
                return self._json({"model": models.resolve(body.get("model"), self.app.runs_dir)})
            if len(parts) == 3 and parts[:2] == ["api", "providers"]:
                from vwb.config import update_provider
                return self._json({"providers": update_provider(parts[2], body)})
            if parts == ["api", "requests"]:
                return self._json(self.app.create(body), 201)
            if parts == ["api", "projects"]:
                return self._json({"project": self.app.projects.create(body.get("name"), operation_id=body.get("operation_id"))}, 201)
            if parts == ["api", "workspaces"]:
                return self._json(self.app.draft_workspace(body), 201)
            if len(parts) == 4 and parts[:2] == ["api", "requests"]:
                ws = self.app.workspace(parts[2])
                action = parts[3]
                if action == "project":
                    return self._json(self.app.move_project(ws, body))
                if action == "history":
                    return self._json(self.app.change_history(ws, body))
                if action == "imports":
                    with self.app.mutation_lock:
                        return self._json({"import": self.app.local_imports.start(ws, body.get("path"))}, 202)
                if action == "browser":
                    with self.app.mutation_lock:
                        return self._json(BrowserSession(ws).ui_action(body))
                if action == "model":
                    return self._json({"selection": models.set_selection(ws, body.get("selection"), self.app.runs_dir),
                                       "applies_to": "next_turn"})
                if action == "prompt":
                    if set(body) != {"profile_id"}:
                        raise ValueError("Choose an agent prompt profile.")
                    prompts.set_selection(ws, body["profile_id"])
                    return self._json(prompts.status(ws, self.app.queue.snapshot(ws).get("active"), self.app.runs_dir))
                if action == "messages":
                    with self.app.mutation_lock:
                        message = self.app.message(body)
                        attachments = self.app.attachment_paths(body.get("attachments", []))
                        refs = chat_attachments.ingest(ws, attachments)
                        job = self.app.queue.enqueue(ws, message=message, mode=body.get("mode", "queue"),
                                                     attachment_refs=refs,
                                                     skip_index=not any(ref["kind"] == "video" for ref in refs),
                                                     context=body.get("context"),
                                                     main_config=models.explicit_runtime(ws, self.app.runs_dir))
                        return self._json({"job": job}, 202)
                if action == "stop":
                    return self._json(self.app.queue.stop(ws))
                if action == "resume":
                    current_model = body.get("use_current_model", False)
                    current_prompt = body.get("use_current_prompt", False)
                    if type(current_model) is not bool or type(current_prompt) is not bool:
                        raise ValueError("use_current_model and use_current_prompt must be booleans.")
                    override = models.explicit_runtime(ws, self.app.runs_dir) if current_model else None
                    # A model-only override keeps the interrupted turn's captured
                    # prompt unless the person also selects the current prompt.
                    if override and not current_prompt:
                        captured = (self.app.queue.snapshot(ws).get("resumable_job") or {}).get("main_config", {}).get("prompt")
                        if captured:
                            override["prompt"] = captured
                        else:
                            row = prompts.profile()
                            override["prompt"] = {key: row[key] for key in ("id", "label", "sha256", "content")}
                    return self._json(self.app.queue.resume(ws, job_id=body.get("job_id"), main_config=override,
                                                          prompt=prompts.snapshot(ws, self.app.runs_dir) if current_prompt else None))
                if action == "answers":
                    return self._json(self.app.answer(ws, body), 202)
                if action == "compact":
                    return self._json({"job": self.app.compact(ws)}, 202)
        elif self.command == "DELETE":
            self._body()
            if len(parts) == 6 and parts[:2] == ["api", "requests"] and parts[3:5] == ["qc", "queue"]:
                from vwb.web.qc import workspace as qc_workspace
                return self._json({"job": self.app.queue.cancel(qc_workspace(self.app.workspace(parts[2])), parts[5])})
            if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3] == "imports":
                return self._json({"import": self.app.local_imports.cancel(self.app.workspace(parts[2]), parts[4])})
            if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3] == "queue":
                return self._json({"job": self.app.queue.cancel(self.app.workspace(parts[2]), parts[4])})
        raise FileNotFoundError("That endpoint was not found.")

    def _callback_uri(self, direct: bool = False, remote: bool = False) -> str:
        provider = "mcp" if remote else "figma-direct" if direct else "solstice"
        return f"{self._request_origin()}/api/integrations/{provider}/callback"

    def _integration_callback(self, query: dict, direct: bool = False, remote: bool = False) -> None:
        ok, pending = False, False
        try:
            if remote:
                if any(len(values) != 1 for values in query.values()):
                    raise IntegrationError("Ambiguous sign-in callback. Reconnect.")
                result = self.app.integrations.remote.complete_auth(
                    {key: values[0] for key, values in query.items()}, self._callback_uri(remote=True))
                pending = isinstance(result, dict) and result.get("state") == "connecting"
            else:
                service = self.app.integrations.direct if direct else self.app.integrations
                service.complete_auth(query.get("code", [""])[0], query.get("state", [""])[0], self._callback_uri(direct=direct))
            ok = True
        except IntegrationError:
            pass  # Provider details, codes and credentials never enter this page.
        nonce = secrets.token_urlsafe(24)
        message = ("Sign-in received. Workbench is finishing the connection." if pending else
                   "Connected. You can return to the Workbench." if ok else
                   "Sign-in was not completed. Return to the Workbench and try again.")
        data = json.dumps({"type": "vwb-integration-auth", "ok": ok})
        payload = (f'<!doctype html><html><head><meta charset="utf-8"><title>Workbench connection</title></head>'
                   f'<body><p>{message}</p><a href="/">Return to Workbench</a><script nonce="{nonce}">'
                   f'if(window.opener){{window.opener.postMessage({data},location.origin);window.close();}}'
                   '</script></body></html>').encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{nonce}'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def _browser_stream(self, session: BrowserSession) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        events = session.stream()
        try:
            for name, payload in events:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # Headers have already been sent. Never append an HTTP error page or
            # expose a CDP endpoint in a stream failure.
            self.wfile.write(b'event: state\ndata: {"error":"The browser stream disconnected. Reconnect to continue."}\n\n')
        finally:
            events.close()

    def _upload(self) -> None:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked uploads are not supported.")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= MAX_UPLOAD:
            raise ValueError("Attach a file no larger than 4 GB.")
        filename = chat_attachments.filename(unquote(self.headers.get("X-Filename", "attachment")))
        upload_id = uuid.uuid4().hex
        self.app.upload_dir.mkdir(parents=True, exist_ok=True)
        upload_folder = self.app.upload_dir / upload_id
        upload_folder.mkdir()
        dest = upload_folder / filename
        remaining = length
        try:
            with dest.open("xb") as fh:
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("The upload was interrupted. Try attaching it again.")
                    fh.write(block)
                    remaining -= len(block)
            from vwb.util.hashing import sha256_file
            metadata = {"id": upload_id, **chat_attachments.describe(dest, filename),
                        "file": f"{upload_id}/{filename}", "size": length, "created": now_iso(),
                        "sha256": sha256_file(dest)}
            write_json(self.app.upload_dir / f"{upload_id}.json", metadata)
        except BaseException:
            dest.unlink(missing_ok=True)
            upload_folder.rmdir()
            raise
        self._json({"upload": {key: value for key, value in metadata.items() if key != "file"}
                    | {"url": f"/api/uploads/{upload_id}"}}, 201)

    def _artifact_html(self, content: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'none'; frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _file(self, path: Path, *, artifact: bool = False, preview: bool = False, handle=None, download: bool = False) -> None:
        if handle is None and not path.is_file():
            raise FileNotFoundError("The file was not found.")
        size = os.fstat(handle.fileno()).st_size if handle is not None else path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            try:
                if not match or not any(match.groups()):
                    raise ValueError
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), end) if right else end
                else:
                    start = max(0, size - int(right))
                if start > end or start >= size or size == 0:
                    raise ValueError
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(status)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if preview and path.suffix.lower() == ".mjs":
            content_type = "text/javascript"
        self.send_header("Content-Type", "application/octet-stream" if download else content_type)
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if preview:
            self.send_header("Access-Control-Allow-Origin", "*")
            # Browser PDF readers cannot load inside a CSP sandbox. The exact
            # PDF MIME type plus nosniff selects that reader, never HTML.
            policy = "default-src 'none'; frame-ancestors 'self'" if content_type == "application/pdf" else "sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'none'; frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'self'; frame-ancestors 'self'"
            self.send_header("Content-Security-Policy", policy)
        elif artifact:
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'")
        else:
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command == "HEAD":
            return
        with nullcontext(handle) if handle is not None else path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _handle(self) -> None:
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PermissionError as exc:
            self.close_connection = True
            self._json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except (QueueConflict, BrowserConflict, ProjectConflict, HistoryConflict) as exc:
            self.close_connection = True
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except IntegrationError as exc:
            self.close_connection = True
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except FileNotFoundError as exc:
            self.close_connection = True
            self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError, KeyError) as exc:
            self.close_connection = True
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            self.close_connection = True
            self._json({"error": "The local server could not finish this request. Try again."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _handle


def serve(*, port: int = 8765, open_browser: bool = True, runs_dir: Path = RUNS_DIR,
          public_url: str | None = None, ngrok: bool = False) -> None:
    from vwb.integrations.callbacks import public_origin
    from vwb.web.public_access import PublicAccess
    from vwb.web.tunnel import Ngrok

    origin = public_origin(public_url)
    previous_origin = os.environ.get('VWB_UI_PUBLIC_URL')
    app = Workbench(runs_dir, public_url=origin)
    server, tunnel = None, None
    previous_signals = {}
    def stop(signum, frame):
        raise KeyboardInterrupt
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGHUP):
                previous_signals[sig] = signal.signal(sig, stop)
        # Bind our own server before starting ngrok. Never tunnel an occupied
        # port belonging to an existing, possibly unprotected Workbench.
        server = WorkbenchServer(("127.0.0.1", port), app)
        if ngrok:
            tunnel = Ngrok(server.server_port, origin)
            origin = tunnel.origin
            app.public_access = PublicAccess(origin)
        if origin:
            os.environ['VWB_UI_PUBLIC_URL'] = origin
            url = app.public_access.launch_url
            print(f"Public Workbench: {origin}\nPrivate launch link: {url}\n"
                  f"Local launch link: http://127.0.0.1:{server.server_port}/#access={app.public_access.launch_token}\n"
                  "Share the launch link only with trusted collaborators. It grants access to this host's Workbench.\n"
                  f"Solstice callback: {origin}/api/integrations/solstice/callback", flush=True)
        else:
            url = f"http://127.0.0.1:{server.server_address[1]}/?qc_access={app.qc_token}"
            print(f"Video Workbench: {url}", flush=True)
        print("Press Ctrl-C to stop the UI and its managed tunnel. Queued messages are kept.", flush=True)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
        if tunnel:
            tunnel.close()
        if server:
            server.server_close()
        app.local_imports.close()
        app.studio.close()
        app.queue.close()
        app.close_browsers()
        if previous_origin is None:
            os.environ.pop('VWB_UI_PUBLIC_URL', None)
        else:
            os.environ['VWB_UI_PUBLIC_URL'] = previous_origin
