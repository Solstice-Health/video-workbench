"""Bounded ripgrep navigation, with current-file hash evidence and scoped reads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .paths import MAX_FILE_BYTES, PathViolation, WorkspacePaths, signature

MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 1024 * 1024
MAX_READ_OUTPUT_BYTES = 512 * 1024
_NOISE = ("node_modules", ".venv", "venv", "__pycache__", ".sandbox", ".cache", ".next", "dist", "build")
_FORBIDDEN = (
    ".git", ".aws", ".ssh", ".gnupg", ".azure", ".kube", ".secrets",
    ".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*service-account*.json",
    "credentials", "credentials.json", "application_default_credentials.json", "secrets.json",
    "secrets.yaml", "secrets.yml", "token.json", "id_rsa", "id_dsa", "id_ecdsa",
    "id_ed25519", ".netrc", ".npmrc", ".pypirc", ".git-credentials", "authorized_keys", "known_hosts",
)


def _integer(args: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def _json_bytes(field: dict[str, Any]) -> bytes:
    if "text" in field:
        return field["text"].encode("utf-8")
    return base64.b64decode(field.get("bytes", ""), validate=True)


def _timeout(args: dict) -> float:
    value = args.get("timeout_s", 10)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 < value <= 30:
        raise ValueError("timeout_s must be greater than zero and at most 30.")
    return float(value)


class SearchEngine:
    def __init__(self, workspace: Path):
        self.paths = WorkspacePaths(workspace)
        self._cache: OrderedDict[str, tuple[tuple, bytes, str]] = OrderedDict()
        self._cache_bytes = 0

    def accept_host_evidence(self, request: dict) -> None:
        """A warmed content cache must not outlive the host's verified bytes.

        The mount fence and search cache are separate. Desktop may preserve old
        attributes briefly even after a forced fence read has seen new content.
        """
        for item in request.get("host_evidence", []):
            cached = self._cache.get(item.get("path"))
            if cached is not None and (item.get("missing") or cached[2] != item.get("sha256")):
                self._cache_bytes -= len(cached[1])
                del self._cache[item["path"]]

    def _snapshot(self, relative: str, *, force: bool = False) -> tuple[bytes, str]:
        with self.paths.open_file(relative) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_FILE_BYTES:
                raise PathViolation("File exceeds the 8 MiB code inspection limit.")
            key = signature(before)
            cached = self._cache.get(relative)
            if not force and cached is not None and cached[0] == key:
                self._cache.move_to_end(relative)
                return cached[1], cached[2]
            raw = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_FILE_BYTES or signature(after) != key:
            raise PathViolation("File changed during inspection or exceeds 8 MiB. Retry.")
        digest = hashlib.sha256(raw).hexdigest()
        if cached is not None:
            self._cache_bytes -= len(cached[1])
        self._cache[relative] = (key, raw, digest)
        self._cache_bytes += len(raw)
        while len(self._cache) > 256 or self._cache_bytes > 32 * 1024 * 1024:
            _, discarded = self._cache.popitem(last=False)
            self._cache_bytes -= len(discarded[1])
        return raw, digest

    def _command(self, args: dict, *, files: bool = False) -> tuple[list[str], str]:
        root = self.paths.resolve(args.get("root", "."))
        relative = self.paths.relative(root)
        include_ignored = bool(args.get("include_ignored", False))
        if include_ignored and (relative == "." or (root.is_dir() and len(Path(relative).parts) < 1)):
            raise ValueError("include_ignored requires a named file or subdirectory, not the workspace root.")
        executable = shutil.which("rg")
        if executable is None:
            raise RuntimeError("ripgrep is unavailable in the code worker; rebuild its runtime image.")
        command = [executable, "--files" if files else "--json", "--hidden", "--no-messages",
                   "--no-config", "--no-require-git", "--color", "never", "--max-filesize", str(MAX_FILE_BYTES)]
        if include_ignored:
            command.append("--no-ignore")
        globs = args.get("globs", args.get("glob", []))
        if isinstance(globs, str):
            globs = [globs]
        if not isinstance(globs, list) or len(globs) > 32:
            raise ValueError("globs must be a list of at most 32 patterns.")
        for value in globs:
            if not isinstance(value, str) or len(value) > 1024 or "\x00" in value:
                raise ValueError("Each glob must be a string of at most 1024 characters.")
            command += ["--glob", value]
        filetypes = args.get("filetypes", args.get("filetype", []))
        if isinstance(filetypes, str):
            filetypes = [filetypes]
        if not isinstance(filetypes, list) or len(filetypes) > 8:
            raise ValueError("filetypes must contain at most eight ripgrep type names.")
        for value in filetypes:
            if not isinstance(value, str) or not value.isalnum():
                raise ValueError("filetype must be a ripgrep type name.")
            command += ["--type", value]
        for name in (() if include_ignored else _NOISE):
            command += ["--glob", f"!**/{name}/**"]
        for name in _FORBIDDEN:
            # Case-insensitive exclusions mirror the post-read path policy.
            command += ["--iglob", f"!**/{name}", "--iglob", f"!**/{name}/**"]
        if not files:
            pattern = args.get("pattern")
            if not isinstance(pattern, str) or len(pattern) > 16384 or "\x00" in pattern:
                raise ValueError("pattern must be a string of at most 16384 characters.")
            if args.get("fixed", args.get("fixed_strings", False)):
                command.append("--fixed-strings")
            if args.get("case_sensitive") is False:
                command.append("--ignore-case")
            if args.get("word"):
                command.append("--word-regexp")
            command += ["--before-context", str(_integer(args, "before", 0, 0, 20)),
                        "--after-context", str(_integer(args, "after", 0, 0, 20)), "--regexp", pattern]
        command += ["--", relative]
        return command, relative

    def _run(self, command: list[str], args: dict) -> tuple[bytes, str | None]:
        timeout = _timeout(args)
        cancel = args.get("cancel_event")
        deadline = time.monotonic() + timeout
        output = bytearray()
        errors = bytearray()
        reason = None
        environment = {"PATH": os.path.dirname(command[0]) + os.pathsep + os.defpath,
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent"}
        process = subprocess.Popen(command, cwd=self.paths.workspace, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                                   start_new_session=True)
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, output)
        selector.register(process.stderr, selectors.EVENT_READ, errors)
        try:
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    reason = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = key.data
                    cap = MAX_CAPTURE_BYTES if target is output else 16384
                    target.extend(chunk[:max(0, cap - len(target))])
                    if target is output and len(output) >= MAX_CAPTURE_BYTES:
                        reason = "output_limit"
                        break
                if reason:
                    break
            if reason:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            returncode = process.wait(timeout=2)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        if returncode not in (0, 1) and reason is None:
            # rg never receives credentials; keep diagnostics bounded.
            raise ValueError(errors.decode("utf-8", errors="replace")[:4000] or "ripgrep failed.")
        return bytes(output), reason

    @staticmethod
    def _page(rows: list[dict], args: dict, reason: str | None, **metadata: Any) -> dict:
        offset = _integer(args, "offset", 0, 0, 100000)
        limit = _integer(args, "limit", 100, 1, 500)
        end = min(offset + limit, len(rows))
        page = []
        used = 0
        for row in rows[offset:end]:
            size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if page and used + size > MAX_PAGE_BYTES:
                break
            page.append(row)
            used += size
        end = offset + len(page)
        more = end < len(rows)
        return {"items": page, "offset": offset, "limit": limit,
                "next_offset": end if more else None, "incomplete": bool(reason),
                "reason": reason, "total": len(rows) if reason is None else None,
                "observed": len(rows), "counts_complete": reason is None,
                "scope": {"max_file_bytes": MAX_FILE_BYTES, "symlinks": "excluded",
                          "credentials": "excluded", "binary_files": "excluded"}, **metadata}

    def files(self, args: dict[str, Any]) -> dict:
        deadline = time.monotonic() + _timeout(args)
        command, root = self._command(args, files=True)
        # NUL separation handles paths containing newlines without ambiguity.
        command.insert(2, "--null")
        raw, reason = self._run(command, args)
        names = raw.split(b"\x00")
        if names[-1]:
            names = names[:-1]
        rows = []
        for encoded in sorted(set(names)):
            if time.monotonic() >= deadline:
                reason = reason or "timeout_during_verification"
                break
            if not encoded:
                continue
            try:
                relative = encoded.decode("utf-8")
                path = self.paths.resolve(relative, kind="file")
                info = path.stat()
                rows.append({"path": self.paths.relative(path), "bytes": info.st_size})
            except (UnicodeError, PathViolation, OSError):
                reason = reason or "workspace_changed_or_unsupported_path"
        return self._page(rows, args, reason, root=root)

    def search(self, args: dict[str, Any]) -> dict:
        deadline = time.monotonic() + _timeout(args)
        started_ns = time.time_ns()
        mode = args.get("mode", "content")
        if mode not in {"content", "files", "count"}:
            raise ValueError("mode must be content, files or count.")
        command, root = self._command(args)
        offset = _integer(args, "offset", 0, 0, 100000)
        limit = _integer(args, "limit", 100, 1, 500)
        raw, reason = self._run(command, args)
        grouped: dict[str, list[dict]] = {}
        canonical: dict[str, str] = {}
        for encoded in raw.split(b"\n"):
            if not encoded:
                continue
            try:
                event = json.loads(encoded)
                if event.get("type") not in {"match", "context"}:
                    continue
                data = event["data"]
                observed_path = _json_bytes(data["path"]).decode("utf-8")
                if observed_path not in canonical:
                    canonical[observed_path] = self.paths.relative(self.paths.resolve(observed_path, kind="file"))
                relative = canonical[observed_path]
                grouped.setdefault(relative, []).append({
                    "line": data["line_number"], "raw": _json_bytes(data["lines"]),
                    "match": event["type"] == "match", "submatches": data.get("submatches", []),
                })
            except (ValueError, UnicodeError, KeyError):
                reason = reason or "invalid_or_truncated_search_output"
        rows = []
        for relative in sorted(grouped):
            if time.monotonic() >= deadline:
                reason = reason or "timeout_during_verification"
                break
            if args.get("cancel_event") is not None and args["cancel_event"].is_set():
                reason = "cancelled"
                break
            try:
                raw_file, digest = self._snapshot(relative)
                # Matching bytes are checked below; a concurrent edit elsewhere in
                # this file may also add unseen matches, so completeness is lost.
                observed_signature = self._cache[relative][0]
                if max(observed_signature[3], observed_signature[4]) > started_ns:
                    reason = reason or "file_changed_during_search"
                if b"\x00" in raw_file:
                    continue
                lines = raw_file.split(b"\n")
                entries = grouped[relative]
                for entry in entries:
                    number = entry["line"]
                    if not isinstance(number, int) or number < 1 or number > len(lines):
                        raise PathViolation("File changed after search.")
                    expected = lines[number - 1] + (b"\n" if number < len(lines) else b"")
                    if entry["raw"] != expected:
                        raise PathViolation("File changed after search.")
                matches = [entry for entry in entries if entry["match"]]
                if mode == "content":
                    by_line = {entry["line"]: entry for entry in entries}
                    for entry in matches:
                        line = entry["raw"].decode("utf-8", errors="replace").rstrip("\r\n")
                        context = []
                        if offset <= len(rows) < offset + limit:
                            for number in range(entry["line"] - args.get("before", 0),
                                                entry["line"] + args.get("after", 0) + 1):
                                item = by_line.get(number)
                                if item is not None and not item["match"]:
                                    context.append({
                                        "line": number,
                                        "text": item["raw"].decode("utf-8", errors="replace").rstrip("\r\n")[:16384],
                                    })
                        rows.append({"path": relative, "line": entry["line"], "text": line[:16384],
                                     "text_truncated": len(line) > 16384, "sha256": digest,
                                     "context": context})
                elif matches:
                    rows.append({"path": relative, "sha256": digest,
                                 "matches": len(matches), "matching_lines": len(matches),
                                 "occurrences": sum(len(entry["submatches"]) for entry in matches),
                                 "counts_complete": reason is None})
            except (PathViolation, OSError):
                reason = reason or "file_changed_during_search"
        # If any later file invalidates completeness, earlier per-file counts must agree.
        if reason:
            for row in rows:
                if "counts_complete" in row:
                    row["counts_complete"] = False
        return self._page(rows, args, reason, root=root, mode=mode)

    def read(self, args: dict[str, Any]) -> dict:
        path = self.paths.resolve(args.get("path", ""), kind="file")
        relative = self.paths.relative(path)
        raw, digest = self._snapshot(relative, force=args.get("expected_sha256") is not None)
        expected = args.get("expected_sha256")
        if expected is not None and expected != digest:
            return {"path": relative, "sha256": digest, "stale": True, "items": [],
                    "reason": "File changed since the supplied SHA-256. Search or read its current version."}
        if b"\x00" in raw:
            raise ValueError("Binary files are not text-readable; use an appropriate inspection tool.")
        offset = _integer(args, "offset", 0, 0, 10000000)
        limit = _integer(args, "limit", 200, 1, 2000)
        byte_limit = _integer(args, "max_bytes", 65536, 1, MAX_READ_OUTPUT_BYTES)
        lines = raw.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        rows = []
        used = 0
        truncated_line = False
        for index in range(offset, min(offset + limit, len(lines))):
            line = lines[index].rstrip(b"\r\n")
            if used + len(line) > byte_limit:
                if not rows:
                    clipped = line[:byte_limit].decode("utf-8", errors="replace")
                    rows.append({"line": index + 1, "text": clipped, "truncated": True})
                    truncated_line = True
                break
            rows.append({"line": index + 1, "text": line.decode("utf-8", errors="replace")})
            used += len(line)
        end = offset + len(rows)
        return {"path": relative, "sha256": digest, "stale": False, "bytes": len(raw),
                "items": rows, "offset": offset, "next_offset": end if end < len(lines) else None,
                "total_lines": len(lines), "incomplete": end < len(lines) or truncated_line,
                "reason": "line_exceeds_output_limit" if truncated_line else None}
