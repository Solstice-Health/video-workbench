"""Bounded host-to-container freshness fencing for read-only code evidence.

Docker Desktop may briefly expose old attributes or bytes after a host edit.
The host records actual hashes, the worker waits for those bytes, and the host
checks returned evidence again. No project code is executed by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections import OrderedDict
from pathlib import Path

from .lsp import LANGUAGES, metadata_manifest, project_root
from .paths import MAX_FILE_BYTES, PathViolation, WorkspacePaths, signature

MAX_HASH_FILES = 2048
MAX_HASH_BYTES = 32 * 1024 * 1024


class FenceLimit(ValueError):
    pass


def _manifest_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stale(response: dict, *, reason: str, path: str | None = None, digest: str | None = None,
           status: str = "stale") -> dict:
    value = {"status": status, "stale": status == "stale", "items": [], "incomplete": True,
             "counts_complete": False, "reason": reason, "next_offset": None}
    if path is not None:
        value["path"] = path
    if digest is not None:
        value["sha256"] = digest
    return {"id": response.get("id"), "result": value}


class HostFence:
    def __init__(self, workspace: Path):
        self.paths = WorkspacePaths(workspace)
        self._hashes: OrderedDict[str, tuple[tuple, str]] = OrderedDict()
        self._files = 0
        self._bytes = 0

    def _budget(self):
        self._files = self._bytes = 0

    def _missing(self, path: str) -> bool:
        parts = self.paths._parts(path)
        if not parts:
            raise PathViolation("An active file must name a workspace-relative file.")
        descriptor = self.paths.open_directory()
        try:
            for index, part in enumerate(parts):
                try:
                    info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return True
                if stat.S_ISLNK(info.st_mode):
                    raise PathViolation("Symlinks are excluded from freshness inspection.")
                if index < len(parts) - 1:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
            return False
        finally:
            os.close(descriptor)

    def _digest(self, path: str, *, force: bool = False) -> str:
        self._files += 1
        if self._files > MAX_HASH_FILES:
            raise FenceLimit("Freshness inspection exceeded 2048 files; choose a smaller project root.")
        with self.paths.open_file(path) as stream:
            before = os.fstat(stream.fileno())
            key = signature(before)
            cached = self._hashes.get(path)
            if not force and cached is not None and cached[0] == key:
                self._hashes.move_to_end(path)
                return cached[1]
            if before.st_size > MAX_FILE_BYTES or self._bytes + before.st_size > MAX_HASH_BYTES:
                raise FenceLimit("Freshness inspection exceeded its 32 MiB budget or 8 MiB file limit; narrow the root.")
            self._bytes += before.st_size
            raw = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_FILE_BYTES or signature(after) != key:
            raise PathViolation("The file changed during freshness inspection; retry.")
        digest = hashlib.sha256(raw).hexdigest()
        self._hashes[path] = (key, digest)
        self._hashes.move_to_end(path)
        while len(self._hashes) > MAX_HASH_FILES * 2:
            self._hashes.popitem(last=False)
        return digest

    def prepare(self, request: dict) -> tuple[dict, dict | None]:
        """Enrich a validated request; caller input cannot assert host evidence."""
        request = {**request}
        request.pop("host_evidence", None)
        request.pop("host_manifest", None)
        args = request.get("arguments", {})
        operation = request.get("operation")
        self._budget()
        evidence = {}
        missing = []
        try:
            if operation in {"read", "lsp"}:
                path = self.paths.relative(self.paths.resolve(args.get("path", ""), kind="file"))
                digest = self._digest(path)
                evidence[path] = digest
                if args.get("expected_sha256") is not None and args["expected_sha256"] != digest:
                    return request, _stale(request, path=path, digest=digest,
                                           reason="The host file changed since the supplied SHA-256. Read its current version.")
            if operation == "context":
                active = args.get("active_files", [])
                if not isinstance(active, list) or len(active) > 8:
                    raise ValueError("active_files must contain at most eight relative paths.")
                for path in active:
                    if self._missing(path):
                        missing.append({"path": Path(path).as_posix(), "missing": True})
                        continue
                    relative = self.paths.relative(self.paths.resolve(path, kind="file"))
                    evidence[relative] = self._digest(relative)
            if operation == "lsp":
                language = LANGUAGES.get(Path(path).suffix.lower())
                if language is None:
                    raise ValueError("This file type has no configured language server.")
                family = "python" if language == "python" else "typescript"
                root = project_root(self.paths, self.paths.resolve(path, kind="file"), family, args.get("root"))
                relative_root = self.paths.relative(root)
                manifest = metadata_manifest(self.paths.workspace, relative_root, family)
                if not manifest["complete"]:
                    raise FenceLimit("Project metadata exceeds the freshness scan bound; choose a smaller root.")
                for relative in sorted(manifest["files"]):
                    if relative not in evidence:
                        evidence[relative] = self._digest(relative)
                # A concurrent rename/addition must not leave a seemingly complete baseline.
                if metadata_manifest(self.paths.workspace, relative_root, family) != manifest:
                    raise PathViolation("The project changed while its freshness baseline was built; retry.")
                request["host_manifest"] = {"root": relative_root, "family": family,
                                            "sha256": _manifest_digest(manifest),
                                            "files": len(manifest["files"])}
        except FenceLimit as exc:
            return request, _stale(request, reason=str(exc), status="unavailable")
        request["host_evidence"] = [{"path": path, "sha256": digest} for path, digest in sorted(evidence.items())] + missing
        if len(json.dumps(request, ensure_ascii=True).encode()) > 900 * 1024:
            return request, _stale(request, reason="Freshness evidence exceeds the request byte limit; narrow the root.",
                                   status="unavailable")
        return request, None

    def wait(self, request: dict, *, timeout_s: float = 2) -> bool:
        """Worker-side: never answer against a mount behind the host baseline."""
        evidence = request.get("host_evidence", [])
        if not isinstance(evidence, list) or len(evidence) > MAX_HASH_FILES:
            raise ValueError("Invalid host evidence.")
        deadline = time.monotonic() + min(2, max(0, timeout_s))
        while True:
            self._budget()
            ready = True
            for item in evidence:
                try:
                    if item.get("missing"):
                        ready = ready and self._missing(item["path"])
                        continue
                    digest = self._digest(item["path"])
                    if digest != item["sha256"]:
                        # A real read can invalidate a Desktop attribute cache,
                        # whereas merely checking unchanged stat values cannot.
                        digest = self._digest(item["path"], force=True)
                    ready = ready and digest == item["sha256"]
                except (OSError, ValueError):
                    ready = False
            expected = request.get("host_manifest")
            if expected is not None:
                actual = metadata_manifest(self.paths.workspace, expected["root"], expected["family"])
                ready = ready and actual["complete"] and _manifest_digest(actual) == expected["sha256"]
            if ready:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def verify(self, request: dict, response: dict) -> dict:
        """Host-side: withhold content if its claimed file version is stale."""
        if "result" not in response:
            return response
        self._budget()
        result = response["result"]
        expected = request.get("host_manifest")
        try:
            if expected is not None:
                actual = metadata_manifest(self.paths.workspace, expected["root"], expected["family"])
                if not actual["complete"] or _manifest_digest(actual) != expected["sha256"]:
                    return _stale(response, reason="The host project changed during language navigation; retry.")
            for item in request.get("host_evidence", []):
                if item.get("missing") and not self._missing(item["path"]):
                    return _stale(response, reason="An active file appeared during Git context inspection; retry.")
            found: dict[str, str] = {item["path"]: item["sha256"] for item in request.get("host_evidence", [])
                                     if not item.get("missing")}

            def visit(value):
                if isinstance(value, dict):
                    path, digest = value.get("path"), value.get("sha256")
                    if isinstance(path, str) and isinstance(digest, str):
                        if path in found and found[path] != digest:
                            raise PathViolation("Mixed file versions were returned.")
                        found[path] = digest
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            visit(result)
            for path, expected_digest in found.items():
                actual_digest = self._digest(path)
                if expected_digest != actual_digest:
                    return _stale(response, path=path, digest=actual_digest,
                                  reason="The host file changed or the browser mount was behind; retry against current bytes.")
            if isinstance(result, dict):
                freshness = result.get("freshness", {})
                freshness = freshness if isinstance(freshness, dict) else {"server": freshness}
                return {**response, "result": {**result, "freshness": {**freshness, "host": {
                    "verified_file_hashes": len(found), "atomic_snapshot": False,
                    "scope": "returned_files_and_lsp_baseline" if expected is not None else "returned_file_hashes_only",
                    "search_absence_verified": False,
                }}}}
            return response
        except (OSError, ValueError) as exc:
            return _stale(response, reason="Current host evidence could not be verified: " + str(exc)[:200],
                          status="unavailable" if isinstance(exc, FenceLimit) else "stale")
