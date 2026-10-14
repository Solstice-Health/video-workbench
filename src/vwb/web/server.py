"""A loopback-only HTTP server for the local workbench.

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
import threading
import uuid
import webbrowser
from contextlib import nullcontext
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from vwb import models
from vwb.agents.events import context_usage, read_events
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
    def __init__(self, runs_dir: Path = RUNS_DIR, *, queue: TurnQueue | None = None):
        self.runs_dir = runs_dir.resolve()
        self.queue = queue or TurnQueue(self.runs_dir)
        self.csrf_token = secrets.token_urlsafe(32)
        self.upload_dir = self.runs_dir / ".ui" / "uploads"
        self.mutation_lock = threading.RLock()
        self.studio = StudioJobs()

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

    def outputs(self, ws: Workspace) -> list[dict[str, Any]]:
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
        return rows

    def snapshot(self, ws: Workspace, *, after: int = 0, full: bool = True) -> dict[str, Any]:
        events = read_events(ws, after=after)
        payload = {"events": events, "cursor": events[-1]["seq"] if events else after,
                   "asks": self.asks(ws), "usage": context_usage(ws), **self.queue.snapshot(ws)}
        if full:
            req = ws.load().model_dump(mode="json")
            for source in req["sources"]:
                source["name"] = Path(source["original_path"]).name
                source["url"] = self.file_url(ws, Path(source["path"]))
                source.pop("original_path", None)
                source.pop("path", None)
            payload.update(request=req, outputs=self.outputs(ws))
        return payload

    def attachment_paths(self, ids: Any) -> list[Path]:
        if not isinstance(ids, list) or len(ids) > 12:
            raise ValueError("Attach up to 12 source videos.")
        paths = []
        for upload_id in ids:
            if not isinstance(upload_id, str) or not re.fullmatch(r"[a-f0-9]{32}", upload_id):
                raise ValueError("Invalid upload id.")
            metadata = read_json(self.upload_dir / f"{upload_id}.json")
            if not metadata:
                raise ValueError("An attachment has expired. Attach it again.")
            path = safe_child(self.upload_dir, metadata["file"])
            if not path.is_file():
                raise ValueError("An attachment is missing. Attach it again.")
            paths.append(path)
        return paths

    @staticmethod
    def message(body: dict[str, Any]) -> str:
        text = body.get("message", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Write a message first.")
        if len(text) > 100_000:
            raise ValueError("The message is too long (maximum 100,000 characters).")
        return text.strip()

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        message = self.message(body)
        context = references.validate(body.get("context"))
        main_config = models.explicit_runtime(runs_dir=self.runs_dir)
        paths = self.attachment_paths(body.get("attachments", []))
        if not paths:
            raise ValueError("Attach a source video to start your conversation.")
        rid = f"r-{uuid.uuid4().hex[:16]}"
        ws = Workspace(self.runs_dir / rid)
        ws.root.mkdir()
        try:
            for sub in SUBDIRS:
                (ws.root / sub).mkdir()
            sources = [ws.add_source(path, f"src{i}") for i, path in enumerate(paths, 1)]
            # The worker adds the first chat message under the turn lock.
            ws.save(Request(id=rid, message=message, sources=sources))
            if main_config:
                models.set_selection(ws, main_config["selection"], self.runs_dir)
            job = self.queue.enqueue(ws, message=message, skip_index=False, context=context, main_config=main_config)
        except BaseException:
            shutil.rmtree(ws.root)
            raise
        return {"request": {"id": rid}, "request_id": rid, "job": job}

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
            if mode == "now" and active and active.get("kind") == "external":
                raise QueueConflict("This turn was started outside the UI. Queue the answer, or stop it in its terminal.")
            # Check the selected provider before signing a human answer. Queue jobs
            # retain these settings even if the person changes the menu afterward.
            main_config = models.explicit_runtime(ws, self.runs_dir)
            label = next((str(o.get("label") or o.get("text") or o["id"]) for o in ticket.get("options", []) if o["id"] == option), option or "")
            message = f"Answer to: {ticket['question']}\n{label}"
            if text.strip():
                message += f"\n{text.strip()}"
            self.message({"message": message})
            answered = broker.answer(tid, option, text=text.strip(), by="local-ui")
            job = self.queue.enqueue(ws, message=message.strip(), mode=mode, main_config=main_config)
            return {"ticket": answered, "job": job}

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
        host = self.headers.get("Host", "")
        if host not in hosts:
            raise PermissionError("Open the workbench at its localhost address.")
        return host

    def _check_origin(self, mutate: bool = False) -> None:
        host = self._check_host()
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            raise PermissionError("Cross-origin requests are not allowed.")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("Cross-site requests are not allowed.")
        if mutate and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.app.csrf_token):
            raise PermissionError("Reload the workbench before making changes.")

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
        if self.command == "GET" and parts == ["api", "integrations", "solstice", "callback"]:
            self._check_host()
            return self._integration_callback(query)
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
        self._check_origin(self.command not in {"GET", "HEAD"})
        if self.command in {"GET", "HEAD"}:
            if parts == ["api", "bootstrap"]:
                return self._json({"csrf_token": self.app.csrf_token, "capabilities": {
                    "upload": True, "queue": True, "steer": "interrupt_then_resume", "compact": "native",
                    "max_upload_bytes": MAX_UPLOAD}})
            if parts == ["api", "requests"]:
                return self._json({"requests": self.app.requests()})
            if parts == ["api", "features"]:
                return self._json(references.catalog())
            if parts == ["api", "models"]:
                return self._json(models.catalog(self.app.runs_dir))
            if parts == ["api", "integrations"]:
                return self._json(self.app.integrations.status())
            if len(parts) in {5, 6} and parts[:2] == ["api", "requests"] and parts[3] == "studio":
                ws = self.app.workspace(parts[2])
                if parts[4] == "capabilities" and len(parts) == 5:
                    return self._json(studio_capabilities())
                if parts[4] == "jobs":
                    if len(parts) == 6:
                        return self._json(self.app.studio_entry(ws, self.app.studio.get(ws, parts[5])))
                    result = self.app.studio.list(ws, query.get("kind", [None])[0])
                    result["jobs"] = [self.app.studio_entry(ws, row) for row in result["jobs"]]
                    return self._json(result)
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
                if len(parts) == 3:
                    return self._json(self.app.snapshot(ws))
                if parts[3] == "events":
                    after = max(0, int(parse_qs(url.query).get("after", ["0"])[0]))
                    return self._json(self.app.snapshot(ws, after=after, full=False))
                if parts[3] == "features":
                    return self._json(references.catalog(ws))
                if parts[3] == "model":
                    selected = models.selection(ws, self.app.runs_dir)
                    return self._json({"selection": selected, "model": models.describe(selected, self.app.runs_dir),
                                       "active_selection": (self.app.queue.snapshot(ws).get("active") or {}).get("main_config", {}).get("selection")})
            if len(parts) >= 3 and parts[0] == "files":
                return self._file(self.app.file(parts[1], "/".join(parts[2:])), artifact=True)
            if parts[0] != "api":
                relative = unquote(url.path).lstrip("/") or "index.html"
                return self._file(safe_child(STATIC_DIR, relative))
        elif self.command == "POST":
            if parts == ["api", "uploads"]:
                return self._upload()
            if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3:] == ["studio", "uploads"]:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Chunked uploads are not supported.")
                ws = self.app.workspace(parts[2])
                result = studio_assets.upload(ws, self.rfile, int(self.headers.get("Content-Length", "0")),
                                              name=unquote(self.headers.get("X-Filename", "input.png")),
                                              kind=self.headers.get("X-Upload-Kind", "image"))
                return self._json(self.app.studio_entry(ws, result), 201)
            body = self._body()
            if len(parts) in {5, 7} and parts[:2] == ["api", "requests"] and parts[3:5] == ["studio", "jobs"]:
                ws = self.app.workspace(parts[2])
                if len(parts) == 5:
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
            if parts == ["api", "models", "resolve"]:
                return self._json({"model": models.resolve(body.get("model"), self.app.runs_dir)})
            if len(parts) == 3 and parts[:2] == ["api", "providers"]:
                from vwb.config import update_provider
                return self._json({"providers": update_provider(parts[2], body)})
            if parts == ["api", "requests"]:
                return self._json(self.app.create(body), 201)
            if len(parts) == 4 and parts[:2] == ["api", "requests"]:
                ws = self.app.workspace(parts[2])
                action = parts[3]
                if action == "browser":
                    return self._json(BrowserSession(ws).ui_action(body))
                if action == "model":
                    return self._json({"selection": models.set_selection(ws, body.get("selection"), self.app.runs_dir),
                                       "applies_to": "next_turn"})
                if action == "messages":
                    attachments = self.app.attachment_paths(body.get("attachments", []))
                    job = self.app.queue.enqueue(ws, message=self.app.message(body), mode=body.get("mode", "queue"),
                                                 attachments=[str(p) for p in attachments], skip_index=not bool(attachments),
                                                 context=body.get("context"),
                                                 main_config=models.explicit_runtime(ws, self.app.runs_dir))
                    return self._json({"job": job}, 202)
                if action == "stop":
                    return self._json(self.app.queue.stop(ws))
                if action == "resume":
                    return self._json(self.app.queue.resume(ws))
                if action == "answers":
                    return self._json(self.app.answer(ws, body), 202)
                if action == "compact":
                    return self._json({"job": self.app.compact(ws)}, 202)
        elif self.command == "DELETE":
            self._body()
            if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3] == "queue":
                return self._json({"job": self.app.queue.cancel(self.app.workspace(parts[2]), parts[4])})
        raise FileNotFoundError("That endpoint was not found.")

    def _callback_uri(self) -> str:
        return f"http://{self._check_host()}/api/integrations/solstice/callback"

    def _integration_callback(self, query: dict) -> None:
        ok = False
        try:
            self.app.integrations.complete_auth(query.get("code", [""])[0], query.get("state", [""])[0], self._callback_uri())
            ok = True
        except IntegrationError:
            pass  # Provider details, codes and credentials never enter this page.
        nonce = secrets.token_urlsafe(24)
        message = "Connected. You can return to the Workbench." if ok else "Sign-in was not completed. Return to the Workbench and try again."
        data = json.dumps({"type": "vwb-integration-auth", "ok": ok})
        payload = (f'<!doctype html><html><head><meta charset="utf-8"><title>Solstice connection</title></head>'
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
        if not 0 < length <= MAX_UPLOAD:
            raise ValueError("Upload a video smaller than 4 GB.")
        filename = unquote(self.headers.get("X-Filename", "video.mp4"))
        filename = Path(filename.replace("\\", "/")).name
        if (Path(filename).suffix.lower() not in VIDEO_TYPES or not filename or len(filename) > 240
                or any(ord(c) < 32 for c in filename)):
            raise ValueError("Attach an MP4, MOV, M4V, MKV or WebM video.")
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
            write_json(self.app.upload_dir / f"{upload_id}.json", {
                "id": upload_id, "name": filename, "file": f"{upload_id}/{filename}", "size": length, "created": now_iso()})
        except BaseException:
            dest.unlink(missing_ok=True)
            upload_folder.rmdir()
            raise
        self._json({"upload": {"id": upload_id, "name": filename, "size": length}}, 201)

    def _file(self, path: Path, *, artifact: bool = False, preview: bool = False, handle=None) -> None:
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
        self.send_header("Content-Type", content_type)
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
        except (QueueConflict, BrowserConflict) as exc:
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

    do_GET = do_HEAD = do_POST = do_DELETE = _handle


def serve(*, port: int = 8765, open_browser: bool = True, runs_dir: Path = RUNS_DIR) -> None:
    app = Workbench(runs_dir)
    try:
        server = WorkbenchServer(("127.0.0.1", port), app)
    except BaseException:
        app.studio.close()
        app.queue.close()
        raise
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Video Workbench: {url}\nPress Ctrl-C to stop the UI. Queued messages are kept.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.studio.close()
        app.queue.close()
        app.close_browsers()
