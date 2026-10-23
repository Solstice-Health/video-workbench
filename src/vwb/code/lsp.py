"""Bounded, read-only language navigation inside the code-intelligence container.

This is a small LSP client, not an editor extension host. It only starts binaries
installed in our image, never project commands, plugins, code actions or package
installers. The host must additionally mount work read-only and disable networking.
Public coordinates are one-based Unicode code-point columns; LSP uses UTF-16.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .paths import PathViolation, WorkspacePaths, signature

MAX_MESSAGE = 8 * 1024**2
MAX_FILE = 1024**2
MAX_OPEN_FILES = 32
MAX_SERVERS = 4
MAX_ROOTS = 2
MAX_STDERR = 16_384
MAX_METADATA_ENTRIES = 30_000
SKIP_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "__pycache__", ".sandbox"}
INSTALL = Path("/opt/vwb/lsp/node_modules")
COMMANDS = {
    "python": (str(INSTALL / ".bin/pyright-langserver"), "--stdio"),
    "typescript": (str(INSTALL / ".bin/typescript-language-server"), "--stdio"),
}
LANGUAGES = {".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescriptreact",
             ".js": "javascript", ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript",
             ".mts": "typescript", ".cts": "typescript"}
OPERATIONS = {"definition": "textDocument/definition", "references": "textDocument/references",
              "hover": "textDocument/hover", "implementation": "textDocument/implementation",
              "document_symbols": "textDocument/documentSymbol", "workspace_symbols": "workspace/symbol",
              "callers": "callHierarchy/incomingCalls", "callees": "callHierarchy/outgoingCalls",
              "diagnostics": "textDocument/diagnostic"}
CAPABILITIES = {"definition": "definitionProvider", "references": "referencesProvider",
                "hover": "hoverProvider", "implementation": "implementationProvider",
                "document_symbols": "documentSymbolProvider", "workspace_symbols": "workspaceSymbolProvider",
                "callers": "callHierarchyProvider", "callees": "callHierarchyProvider"}
SETTINGS = {"python": {"pythonPath": "/usr/bin/python3",
                       "analysis": {"diagnosticMode": "openFilesOnly", "autoSearchPaths": True,
                                    "typeCheckingMode": "basic", "logLevel": "Error"}},
            "pyright": {"disableOrganizeImports": True},
            "typescript": {"validate": {"enable": True}}, "javascript": {"validate": {"enable": True}}}


class LspError(RuntimeError):
    """A bounded server/protocol failure. No untrusted server text in the message."""


class RpcError(LspError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"Language server returned JSON-RPC error {code}.")


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    return value


def _lines(text: str) -> list[str]:
    # LSP treats LF/CRLF/CR as line breaks, not Unicode paragraph separators.
    return re.split(r"\r\n|\r|\n", text)


def to_lsp_position(text: str, line: int, column: int) -> dict[str, int]:
    lines = _lines(text)
    _integer(line, "line", 1, len(lines))
    _integer(column, "column", 1, len(lines[line - 1]) + 1)
    prefix = lines[line - 1][:column - 1]
    return {"line": line - 1, "character": len(prefix.encode("utf-16-le")) // 2}


def from_lsp_position(text: str | None, position: dict) -> dict[str, int]:
    line = _integer(position.get("line"), "server line", 0, 10_000_000)
    units = _integer(position.get("character"), "server character", 0, 10_000_000)
    if text is None:
        return {"line": line + 1, "column": units + 1, "column_encoding": "utf-16"}
    lines = _lines(text)
    if line >= len(lines):
        return {"line": line + 1, "column": units + 1, "column_encoding": "utf-16"}
    consumed, column = 0, 0
    for char in lines[line]:
        width = 2 if ord(char) > 0xFFFF else 1
        if consumed + width > units:
            break
        consumed += width
        column += 1
    return {"line": line + 1, "column": column + 1}


@dataclass
class Document:
    text: str
    sha256: str
    version: int = 1


@dataclass
class _Snapshot:
    files: dict[str, tuple[int, int, int, int, int]]
    complete: bool


def project_root(paths: WorkspacePaths, path: Path, family: str, root: str | None = None) -> Path:
    """Resolve a nested project without starting a process or evaluating config."""
    if family not in {"python", "typescript"}:
        raise ValueError("family must be python or typescript.")
    if root is not None:
        project = paths.resolve(root, kind="directory")
        if path != project and project not in path.parents:
            raise ValueError("The requested project root must contain path.")
        return project
    markers = ("pyrightconfig.json", "pyproject.toml", "setup.cfg", "setup.py") if family == "python" else (
        "tsconfig.json", "jsconfig.json", "package.json")
    for candidate in (path.parent, *path.parents[1:]):
        if candidate != paths.workspace and paths.workspace not in candidate.parents:
            break
        if any((candidate / marker).is_file() and not (candidate / marker).is_symlink() for marker in markers):
            return candidate
    return paths.workspace


class _Server:
    """One stdio server. A reader thread correlates responses and notifications."""

    def __init__(self, root: Path, family: str, command: tuple[str, ...], deadline: float):
        self.root, self.family = root, family
        self.opened: OrderedDict[str, Document] = OrderedDict()
        self.diagnostics: dict[str, dict] = {}
        self.capabilities: dict = {}
        self.sequence, self.next_id = 0, 0
        self.responses: dict[int, dict | None] = {}
        self.failure: str | None = None
        self.stderr = bytearray()
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.last_used = time.monotonic()
        self.config_hashes: dict[str, str] = {}
        self.snapshot = _Snapshot({}, False)
        self.typescript_version: dict | None = None
        # Deliberately do not inherit NODE_OPTIONS, PYTHONPATH or any vendor key.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
               "LANG": "C.UTF-8", "NO_COLOR": "1", "PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1"}
        try:
            self.process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            start_new_session=True, bufsize=0)
        except OSError as exc:
            raise LspError(f"The pinned {family} language server is unavailable in the image.") from exc
        os.set_blocking(self.process.stdin.fileno(), False)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.reader.start()
        self.err_reader.start()
        try:
            initialization = {"processId": os.getpid(), "rootUri": root.as_uri(),
                              "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                              "clientInfo": {"name": "Video Workbench read-only navigation", "version": "1"},
                              "capabilities": {"general": {"positionEncodings": ["utf-16"]},
                                               "workspace": {"configuration": True, "workspaceFolders": True},
                                               "textDocument": {"synchronization": {"didSave": True},
                                                                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                                                "publishDiagnostics": {"versionSupport": True},
                                                                "callHierarchy": {"dynamicRegistration": False}}}}
            if family == "typescript":
                initialization["initializationOptions"] = {
                    "hostInfo": "Video Workbench", "disableAutomaticTypingAcquisition": True,
                    "plugins": [], "maxTsServerMemory": 512,
                    "tsserver": {"path": str(INSTALL / "typescript/lib/tsserver.js"),
                                 "fallbackPath": str(INSTALL / "typescript/lib/tsserver.js"),
                                 "useSyntaxServer": "never", "logVerbosity": "off"},
                    "preferences": {"includePackageJsonAutoImports": "off"},
                }
            result = self.request("initialize", initialization, deadline)
            self.capabilities = result.get("capabilities", {}) if isinstance(result, dict) else {}
            encoding = self.capabilities.get("positionEncoding", "utf-16")
            if encoding != "utf-16":
                raise LspError("Language server negotiated an unsupported position encoding.")
            self.notify("initialized", {}, deadline)
            self.notify("workspace/didChangeConfiguration", {"settings": SETTINGS}, deadline)
        except BaseException:
            self.close(graceful=False)
            raise

    def _send(self, message: dict, deadline: float):
        data = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_MESSAGE:
            raise LspError("Language server request exceeded the message budget.")
        packet = memoryview(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self.write_lock.acquire(timeout=remaining):
            raise TimeoutError("Language server write timed out.")
        try:
            while packet:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Language server write timed out.")
                if self.process.poll() is not None:
                    raise LspError("Language server exited before replying.")
                if not select.select([], [self.process.stdin.fileno()], [], remaining)[1]:
                    raise TimeoutError("Language server write timed out.")
                try:
                    count = os.write(self.process.stdin.fileno(), packet)
                    packet = packet[count:]
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise LspError("Language server input closed.") from exc
        finally:
            self.write_lock.release()

    def notify(self, method: str, params: dict, deadline: float):
        self._send({"method": method, "params": params}, deadline)

    def request(self, method: str, params: dict | None, deadline: float) -> Any:
        with self.condition:
            self.next_id += 1
            request_id = self.next_id
            self.responses[request_id] = None
        try:
            self._send({"id": request_id, "method": method, "params": params}, deadline)
            with self.condition:
                while self.responses[request_id] is None:
                    if self.failure:
                        raise LspError(self.failure)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"Language server {method} timed out.")
                    self.condition.wait(remaining)
                response = self.responses[request_id]
            if "error" in response:
                raise RpcError(response["error"].get("code", -32603))
            return response.get("result")
        finally:
            with self.condition:
                self.responses.pop(request_id, None)

    def _read(self):
        stream = self.process.stdout
        try:
            while True:
                length, header_size = None, 0
                while True:
                    header = stream.readline(4097)
                    if not header:
                        raise EOFError
                    header_size += len(header)
                    if len(header) > 4096 or header_size > 16_384:
                        raise ValueError("header budget")
                    if header in (b"\r\n", b"\n"):
                        break
                    key, separator, value = header.partition(b":")
                    if not separator:
                        raise ValueError("invalid header")
                    if key.lower() == b"content-length":
                        if length is not None:
                            raise ValueError("duplicate length")
                        length = int(value.strip())
                if length is None or not 0 < length <= MAX_MESSAGE:
                    raise ValueError("message budget")
                chunks, left = [], length
                while left:
                    chunk = stream.read(left)
                    if not chunk:
                        raise EOFError
                    chunks.append(chunk)
                    left -= len(chunk)
                message = json.loads(b"".join(chunks))
                if not isinstance(message, dict):
                    raise ValueError("invalid object")
                if "method" in message:
                    self._incoming(message)
                elif isinstance(message.get("id"), int):
                    with self.condition:
                        if message["id"] in self.responses:
                            self.responses[message["id"]] = message
                        self.condition.notify_all()
        except (OSError, ValueError, EOFError, TypeError, LspError, TimeoutError, RecursionError):
            with self.condition:
                self.failure = "Language server closed or returned an invalid protocol message."
                self.condition.notify_all()

    def _read_stderr(self):
        try:
            while chunk := self.process.stderr.read(4096):
                self.stderr.extend(chunk)
                del self.stderr[:-MAX_STDERR]
        except OSError:
            pass

    def _incoming(self, message: dict):
        method, params = message.get("method"), message.get("params") or {}
        if "id" in message:
            result: Any = None
            if method == "workspace/configuration":
                result = []
                for item in params.get("items", [])[:100]:
                    value = SETTINGS
                    for component in item.get("section", "").split("."):
                        value = value.get(component, {}) if isinstance(value, dict) else {}
                    result.append(value)
            elif method == "workspace/workspaceFolders":
                result = [{"uri": self.root.as_uri(), "name": self.root.name}]
            elif method == "workspace/applyEdit":
                result = {"applied": False, "failureReason": "Read-only navigation client."}
            self._send({"id": message["id"], "result": result}, time.monotonic() + 1)
        elif method == "$/typescriptVersion":
            # This is the server's acknowledgement of which TypeScript it
            # selected. The initialization path must win over a project copy.
            if params.get("source") == "workspace":
                raise LspError("Language server selected a project TypeScript binary instead of the pinned image binary.")
            self.typescript_version = {"version": str(params.get("version", ""))[:100],
                                       "source": str(params.get("source", ""))[:100]}
        elif method == "textDocument/publishDiagnostics":
            uri = params.get("uri")
            if not isinstance(uri, str):
                return
            with self.condition:
                document = self.opened.get(uri)
                version = params.get("version")
                if document is None or (version is not None and version != document.version):
                    return
                self.sequence += 1
                self.diagnostics[uri] = {"version": version, "sequence": self.sequence,
                                         "received_at": time.monotonic(),
                                         "items": params.get("diagnostics", [])}
                self.condition.notify_all()

    def close(self, graceful=True):
        process = self.process
        if graceful and process.poll() is None:
            try:
                deadline = time.monotonic() + 0.5
                self.request("shutdown", None, deadline)
                self.notify("exit", {}, deadline)
                process.wait(timeout=0.2)
            except (LspError, TimeoutError, OSError, subprocess.TimeoutExpired):
                pass
        # Terminate the complete owned session, including tsserver subprocesses.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        self.reader.join(timeout=0.2)
        self.err_reader.join(timeout=0.2)


class LspPool:
    """At most four servers / two nested project roots for one request workspace.

    The private ``_commands`` argument is solely a protocol-test seam. It is never
    accepted by worker RPC or tool arguments. Production requires Docker and the
    fixed immutable installation above; no host-side subprocess fallback exists.
    """

    def __init__(self, workspace: Path, *, _commands: dict[str, tuple[str, ...]] | None = None):
        self.workspace = workspace.resolve()
        self.paths = WorkspacePaths(workspace)
        self._commands = _commands
        self.servers: OrderedDict[tuple[str, str], _Server] = OrderedDict()
        self.host_evidence: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        self.lock = threading.RLock()
        self.closed = False

    def status(self) -> dict:
        with self.lock:
            return {"mode": "read_only", "coordinates": "one_based_unicode_code_points",
                    "languages": ["python", "typescript", "tsx", "javascript", "jsx"],
                    "max_servers": MAX_SERVERS, "max_roots": MAX_ROOTS,
                    "servers": [{"root": self.paths.relative(server.root), "language": family,
                                 "opened_files": len(server.opened), "alive": server.process.poll() is None}
                                for (_, family), server in self.servers.items()]}

    def accept_host_evidence(self, request: dict) -> bool:
        """Invalidate warm semantic state using trusted host *content* versions.

        Called by the fixed worker only after its host/container fence succeeds.
        Container attributes can lag actual bytes, so a metadata-only comparison
        cannot establish that an unopened dependency stayed unchanged. This
        baseline is not accepted as a tool argument or treated as user authority.
        """
        manifest = request.get("host_manifest")
        if request.get("operation") != "lsp" or manifest is None:
            return False
        if not isinstance(manifest, dict) or manifest.get("family") not in COMMANDS:
            raise ValueError("Invalid host language manifest.")
        root = self.paths.resolve(manifest.get("root", "."), kind="directory")
        evidence = request.get("host_evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 2048:
            raise ValueError("Host language evidence exceeds its file bound.")
        current = {}
        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError("Invalid host language evidence item.")
            path = item.get("path")
            if not isinstance(path, str):
                raise ValueError("Host language evidence requires relative file paths.")
            # The input file and complete project manifest have already passed
            # the secure host path checks. Repeat lexical boundary checks here.
            self.paths.relative(self.workspace / path)
            digest = item.get("sha256")
            if item.get("missing") is True:
                digest = "missing"
            elif not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("Host language evidence requires SHA-256 digests.")
            current[path] = digest
        key = (str(root), manifest["family"])
        with self.lock:
            previous = self.host_evidence.get(key)
            invalidated = key in self.servers and previous != current
            if invalidated:
                self.servers.pop(key).close(graceful=False)
            self.host_evidence[key] = current
            self.host_evidence.move_to_end(key)
            while len(self.host_evidence) > MAX_SERVERS:
                self.host_evidence.popitem(last=False)
            return invalidated

    def _document(self, relative: str) -> Document:
        raw, _ = self.paths.read_bytes(relative, max_bytes=MAX_FILE)
        if b"\x00" in raw:
            raise ValueError("Language navigation accepts UTF-8 source text, not binary data.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Language navigation requires UTF-8 source text.") from exc
        return Document(text, hashlib.sha256(raw).hexdigest())

    def _project(self, path: Path, family: str, root: str | None) -> Path:
        return project_root(self.paths, path, family, root)

    def _configs(self, root: Path, family: str) -> dict[str, str]:
        names = ("pyrightconfig.json", "pyproject.toml", "setup.cfg") if family == "python" else (
            "tsconfig.json", "jsconfig.json", "package.json")
        results = {}
        for name in names:
            candidate = root / name
            if not candidate.exists() and not candidate.is_symlink():
                continue
            try:
                relative = self.paths.relative(candidate)
                data, _ = self.paths.read_bytes(relative, max_bytes=MAX_FILE)
                results[name] = hashlib.sha256(data).hexdigest()
            except FileNotFoundError:
                continue
        return results

    def _snapshot(self, root: Path, family: str) -> _Snapshot:
        """Metadata, not a second content index. Detect server-cache invalidation.

        Never scan dependencies or follow links. An omitted target is labelled
        explicitly rather than presenting its new disk hash as semantic proof.
        """
        extensions = {".py", ".pyi"} if family == "python" else set(LANGUAGES) - {".py", ".pyi"}
        configs = {"pyrightconfig.json", "pyproject.toml", "setup.cfg"} if family == "python" else {
            "tsconfig.json", "jsconfig.json", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
        stack = [self.paths.relative(root)]
        files, scanned, complete = {}, 0, True
        while stack:
            relative = stack.pop()
            try:
                descriptor = self.paths.open_directory(relative)
            except (OSError, PathViolation):
                complete = False
                continue
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_METADATA_ENTRIES:
                            return _Snapshot(files, False)
                        path = (Path(relative) / entry.name).as_posix()
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in SKIP_DIRECTORIES:
                                if len(Path(path).parts) > 32:
                                    complete = False
                                else:
                                    stack.append(path)
                        elif (Path(entry.name).suffix in extensions or entry.name in configs) and entry.is_file(follow_symlinks=False):
                            # relative() checks sensitive path components even
                            # for files omitted by ordinary ignore rules.
                            try:
                                self.paths.relative(self.workspace / path)
                                files[path] = signature(entry.stat(follow_symlinks=False))
                            except (OSError, PathViolation):
                                continue
            except OSError:
                complete = False
            finally:
                os.close(descriptor)
        return _Snapshot(files, complete)

    def _server(self, root: Path, family: str, deadline: float, snapshot: _Snapshot) -> _Server:
        key = (str(root), family)
        configs = self._configs(root, family)
        existing = self.servers.get(key)
        unseen_changes = False
        if existing:
            changed = {path for path in existing.snapshot.files.keys() | snapshot.files.keys()
                       if existing.snapshot.files.get(path) != snapshot.files.get(path)}
            opened = {self.paths.relative(Path(unquote(urlparse(uri).path))) for uri in existing.opened}
            unseen_changes = bool(changed - opened) or not existing.snapshot.complete or not snapshot.complete
        if existing and (existing.failure or existing.process.poll() is not None
                         or existing.config_hashes != configs or unseen_changes):
            self.servers.pop(key).close(graceful=False)
            existing = None
        if existing:
            self.servers.move_to_end(key)
            return existing
        if self._commands is None and not Path("/.dockerenv").is_file():
            raise LspError("Language servers run only in the isolated code-intelligence Docker worker.")
        roots = {entry[0] for entry in self.servers}
        while self.servers and (len(self.servers) >= MAX_SERVERS or (str(root) not in roots and len(roots) >= MAX_ROOTS)):
            oldest_key = next(iter(self.servers))
            evicted_root = oldest_key[0]
            for candidate in list(self.servers):
                if candidate[0] == evicted_root:
                    self.servers.pop(candidate).close()
            roots = {entry[0] for entry in self.servers}
        commands = self._commands if self._commands is not None else COMMANDS
        server = _Server(root, family, commands[family], deadline)
        server.config_hashes = configs
        server.snapshot = snapshot
        self.servers[key] = server
        return server

    def _sync(self, deadline: float):
        for server in self.servers.values():
            updates: list[tuple[str, Document | None]] = []
            for uri, current in list(server.opened.items()):
                relative = self.paths.relative(Path(unquote(urlparse(uri).path)))
                try:
                    document = self._document(relative)
                except (FileNotFoundError, PathViolation):
                    updates.append((uri, None))
                    continue
                if current.sha256 != document.sha256:
                    document.version = current.version + 1
                    updates.append((uri, document))
            if updates:
                # A changed dependency may invalidate diagnostics in another
                # opened file whose own document version has not changed.
                server.diagnostics.clear()
            changes = []
            for uri, document in updates:
                if document is None:
                    server.notify("textDocument/didClose", {"textDocument": {"uri": uri}}, deadline)
                    server.opened.pop(uri, None)
                    changes.append({"uri": uri, "type": 3})
                    continue
                server.opened[uri] = document
                server.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": document.version},
                                                        "contentChanges": [{"text": document.text}]}, deadline)
                changes.append({"uri": uri, "type": 2})
            if changes:
                server.notify("workspace/didChangeWatchedFiles", {"changes": changes}, deadline)

    def _open(self, server: _Server, path: Path, document: Document, deadline: float) -> Document:
        uri = path.as_uri()
        if uri in server.opened:
            server.opened.move_to_end(uri)
            return server.opened[uri]
        if len(server.opened) >= MAX_OPEN_FILES:
            old_uri, _ = server.opened.popitem(last=False)
            server.diagnostics.pop(old_uri, None)
            server.notify("textDocument/didClose", {"textDocument": {"uri": old_uri}}, deadline)
        server.opened[uri] = document
        server.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": LANGUAGES[path.suffix],
                                                               "version": document.version, "text": document.text}}, deadline)
        return document

    def call(self, args: dict) -> dict:
        operation = args.get("operation")
        if operation not in OPERATIONS:
            raise ValueError(f"operation must be one of {', '.join(OPERATIONS)}.")
        timeout = args.get("timeout_s", 20)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ValueError("timeout_s must be greater than zero and at most 30.")
        limit = _integer(args.get("limit", 100), "limit", 1, 200)
        path = self.paths.resolve(args.get("path", ""), kind="file")
        if path.suffix not in LANGUAGES:
            raise ValueError("Use a Python, JavaScript, TypeScript or TSX source file.")
        family = "python" if LANGUAGES[path.suffix] == "python" else "typescript"
        root = self._project(path, family, args.get("root"))
        relative = self.paths.relative(path)
        document = self._document(relative)
        expected = args.get("expected_sha256")
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest.")
        if expected is not None and document.sha256 != expected:
            return {"status": "stale", "operation": operation, "path": relative, "sha256": document.sha256,
                    "message": "Source changed. Read the current file and repeat with its current hash."}
        query = args.get("query", "")
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError("query must be text of at most 256 characters.")
        deadline = time.monotonic() + timeout
        with self.lock:
            if self.closed:
                raise LspError("Language navigation pool is closed.")
            snapshot = self._snapshot(root, family)
            server = self._server(root, family, deadline, snapshot)
            server.snapshot = snapshot
            self._active_snapshot = snapshot
            try:
                self._sync(deadline)
                document = self._open(server, path, document, deadline)
                if expected is not None and document.sha256 != expected:
                    return {"status": "stale", "operation": operation, "path": relative, "sha256": document.sha256,
                            "message": "Source changed before navigation; read it again."}
                position = None
                if operation not in {"document_symbols", "workspace_symbols", "diagnostics"}:
                    position = to_lsp_position(document.text, args.get("line", 1), args.get("column", 1))
                server.last_used = time.monotonic()
                base = {"status": "ok", "operation": operation, "language": family, "path": relative,
                        "root": self.paths.relative(root), "sha256": document.sha256, "version": document.version,
                        "coordinates": "one_based_unicode_code_points", "source": "disk"}
                base["workspace_snapshot_complete"] = snapshot.complete
                capability = CAPABILITIES.get(operation)
                if capability and not server.capabilities.get(capability):
                    return {**base, "status": "unsupported", "items": [],
                            "message": f"The pinned {family} server does not advertise {operation}."}
                if operation == "diagnostics":
                    result = self._diagnostics(server, path, base, limit, deadline)
                    if snapshot.files != self._snapshot(root, family).files:
                        return {**base, "status": "stale", "items": [], "complete": False,
                                "message": "Project files changed during diagnostics; repeat the request."}
                    return result
                params = {"textDocument": {"uri": path.as_uri()}}
                if position is not None:
                    params["position"] = position
                if operation == "references":
                    params["context"] = {"includeDeclaration": bool(args.get("include_declaration", True))}
                if operation == "workspace_symbols":
                    params = {"query": query}
                if operation in {"callers", "callees"}:
                    prepared = server.request("textDocument/prepareCallHierarchy", params, deadline) or []
                    raw = []
                    for item in prepared[:5]:
                        raw.extend({**call, "_source_uri": item.get("uri", path.as_uri())}
                                   for call in server.request(OPERATIONS[operation], {"item": item}, deadline) or [])
                else:
                    raw = server.request(OPERATIONS[operation], params, deadline)
                self._location_cache: dict[str, Document] = {}
                result = self._normalize(operation, raw, path, limit)
                if server.typescript_version:
                    base["typescript"] = server.typescript_version
                latest = self._document(relative)
                after = self._snapshot(root, family)
                if latest.sha256 != document.sha256 or snapshot.files != after.files:
                    return {**base, "status": "stale", "items": [], "current_sha256": latest.sha256,
                            "message": "Project files changed during navigation; repeat against the current files."}
                return {**base, **result}
            except RpcError as exc:
                if exc.code == -32601:
                    return {"status": "unsupported", "operation": operation, "path": relative, "items": []}
                raise
            except (TimeoutError, LspError):
                self.servers.pop((str(root), family), None)
                server.close(graceful=False)
                raise

    def _clean(self, text: Any, maximum=4000) -> str:
        value = str(text).replace(str(self.workspace), ".")[:maximum]
        value = re.sub(r"file://[^\s`<>\]\)]+", "[external library]", value)
        return value

    def _location(self, uri: Any, region: dict | None, role="reference") -> dict:
        if not isinstance(uri, str):
            return {"role": "unresolved", "label": "Location was not provided by the server."}
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            return {"role": "external_library", "label": "External library"}
        path = Path(unquote(parsed.path))
        try:
            relative = self.paths.relative(path)
            self.paths.resolve(relative, kind="file")
            cache = getattr(self, "_location_cache", {})
            document = cache.get(relative)
            if document is None:
                document = self._document(relative)
                cache[relative] = document
        except (ValueError, OSError):
            return {"role": "external_library", "label": "External library"}
        result = {"path": relative, "sha256": document.sha256, "role": role}
        active = getattr(self, "_active_snapshot", _Snapshot({}, False))
        if relative not in active.files:
            result["freshness"] = "outside_project_snapshot"
        if region:
            result["range"] = {side: from_lsp_position(document.text, region[side]) for side in ("start", "end")}
        return result

    def _normalize(self, operation: str, raw: Any, path: Path, limit: int) -> dict:
        if operation == "hover":
            if not raw:
                return {"items": []}
            contents = raw.get("contents", "")
            values = contents if isinstance(contents, list) else [contents]
            text = "\n\n".join(self._clean(value.get("value", "") if isinstance(value, dict) else value)
                               for value in values)
            return {"items": [{**self._location(path.as_uri(), raw.get("range"), "hover"),
                               "text": text[:16_000]}], "truncated": len(text) > 16_000}
        rows: list[dict] = []
        truncated = False

        def add(item: dict, container: str | None = None):
            nonlocal truncated
            if len(rows) >= limit:
                truncated = True
                return
            if operation in {"callers", "callees"}:
                target = item.get("from" if operation == "callers" else "to", {})
                row = self._location(target.get("uri"), target.get("selectionRange") or target.get("range"),
                                     "caller" if operation == "callers" else "callee")
                row.update(name=self._clean(target.get("name", ""), 500), kind=target.get("kind"))
                site_uri = target.get("uri") if operation == "callers" else item.get("_source_uri", path.as_uri())
                row["call_sites"] = [self._location(site_uri, region, "call_site")
                                     for region in item.get("fromRanges", [])[:20]]
            elif operation in {"workspace_symbols", "document_symbols"}:
                location = item.get("location", {})
                row = self._location(location.get("uri", path.as_uri()),
                                     location.get("range") or item.get("selectionRange") or item.get("range"), "symbol")
                row.update(name=self._clean(item.get("name", ""), 500), kind=item.get("kind"))
                if item.get("detail"):
                    row["detail"] = self._clean(item["detail"], 2000)
                if container or item.get("containerName"):
                    row["container"] = self._clean(container or item["containerName"], 500)
            else:
                row = self._location(item.get("uri", item.get("targetUri")),
                                     item.get("range", item.get("targetSelectionRange", item.get("targetRange"))),
                                     "definition" if operation in {"definition", "implementation"} else "reference")
            rows.append(row)
            for child in item.get("children", []):
                add(child, item.get("name"))

        values = raw if isinstance(raw, list) else [raw] if raw else []
        for value in values:
            if isinstance(value, dict):
                add(value)
        return {"items": rows, "truncated": truncated}

    def _diagnostics(self, server: _Server, path: Path, base: dict, limit: int, deadline: float) -> dict:
        uri = path.as_uri()
        version = server.opened[uri].version
        if server.capabilities.get("diagnosticProvider"):
            report = server.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}, deadline) or {}
            diagnostics = {"version": version, "items": report.get("items", []), "pull": True}
        else:
            # Navigation barrier makes the server process didOpen/didChange first;
            # diagnostics can still arrive later and are bounded by this call's deadline.
            if server.capabilities.get("hoverProvider"):
                server.request("textDocument/hover", {"textDocument": {"uri": uri},
                                                       "position": {"line": 0, "character": 0}}, deadline)
            with server.condition:
                while uri not in server.diagnostics:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {**base, "status": "pending", "items": [], "complete": False,
                                "message": "No current diagnostics arrived before the deadline; this is not a clean bill of health."}
                    if server.failure:
                        raise LspError(server.failure)
                    server.condition.wait(remaining)
                diagnostics = server.diagnostics[uri]
        current = self._document(self.paths.relative(path))
        if current.sha256 != base["sha256"]:
            return {**base, "status": "stale", "items": [], "complete": False,
                    "current_sha256": current.sha256}
        rows = []
        self._location_cache = {}
        for item in diagnostics.get("items", [])[:limit]:
            row = self._location(uri, item.get("range"), "diagnostic")
            row.update(message=self._clean(item.get("message", "")), severity=item.get("severity"),
                       code=self._clean(item.get("code", ""), 100), source=self._clean(item.get("source", ""), 100))
            rows.append(row)
        versioned = diagnostics.get("version") == version
        return {**base, "items": rows, "complete": versioned and len(diagnostics.get("items", [])) <= limit,
                "diagnostic_version": diagnostics.get("version"),
                "freshness": "server_version" if versioned else "unversioned_notification",
                "truncated": len(diagnostics.get("items", [])) > limit,
                "message": "" if versioned else "The server supplied no document version; completeness cannot be established."}

    def close(self):
        with self.lock:
            self.closed = True
            for server in self.servers.values():
                server.close()
            self.servers.clear()
            self.host_evidence.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def metadata_manifest(workspace: Path, root: str = ".", family: str = "python") -> dict:
    """Portable host/container fence evidence; never starts a language server.

    Device, inode and ctime intentionally stay out of this contract because host
    and Docker bind-mount filesystems do not necessarily share those values.
    """
    if family not in {"python", "typescript"}:
        raise ValueError("family must be python or typescript.")
    pool = LspPool(workspace)
    project = pool.paths.resolve(root, kind="directory")
    snapshot = pool._snapshot(project, family)
    return {"files": {path: {"size": value[2], "mtime_ns": value[3]}
                       for path, value in snapshot.files.items()}, "complete": snapshot.complete}
