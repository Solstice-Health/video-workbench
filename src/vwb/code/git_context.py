"""Bounded, read-only Git context for an explicitly authorized workspace.

These operations run in the code worker, with a read-only work mount and no
network. No repository-defined command, pager, hook, textconv or diff driver is
executed. Paths in results are relative to the mounted workspace.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import PathViolation, WorkspacePaths, is_sensitive_path

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@{}^~+\-]{0,199}\Z")
_TIMEOUT = 12.0
_SCAN_BYTES = 2 * 1024 * 1024
_OUTPUT_BYTES = 48_000


@dataclass(frozen=True)
class _Repository:
    root: Path
    git_dir: Path
    selected: Path
    scope: str


def _integer(value: Any, default: int, low: int, high: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}.")
    return value


def _relative(value: Any, name: str = "path") -> str:
    if value is None:
        return "."
    if (not isinstance(value, str) or not value or len(value) > 2048 or value.startswith("/")
            or "\\" in value or "\x00" in value or ".." in value.split("/")):
        raise ValueError(f"{name} must be a relative workspace path without traversal.")
    return str(Path(value))


class GitContext:
    """Queries a Git checkout without initializing or changing it.

    ``root`` selects an authorized project directory. ``path`` is relative to
    that directory. Discovery never walks above the work mount. A workspace may
    therefore contain several independent imported repositories.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.paths = WorkspacePaths(workspace)
        # The worker supplies a trusted image; never use a repository's executable.
        self.git = next((str(p) for p in (Path("/usr/bin/git"), Path("/usr/local/bin/git"))
                         if p.is_file() and os.access(p, os.X_OK)), None)
        if self.git is None:
            candidate = shutil.which("git", path="/usr/bin:/bin:/usr/local/bin")
            self.git = candidate

    def _metadata_path(self, candidate: Path) -> Path:
        """Reject external pointers and symlinks before handing metadata to Git."""
        candidate = Path(os.path.abspath(candidate))
        if not candidate.is_relative_to(self.workspace):
            raise PermissionError("Git metadata points outside the authorized workspace.")
        current = self.workspace
        for part in candidate.relative_to(self.workspace).parts:
            current = current / part
            if current.is_symlink():
                raise PermissionError("Git metadata cannot contain symbolic links.")
        return candidate

    def _repository(self, args: dict) -> _Repository | None:
        selected = self.paths.resolve(_relative(args.get("root"), "root"), kind="directory")
        scope_path = _relative(args.get("path"))
        scope = selected / scope_path
        relative_scope = str(scope.relative_to(self.workspace))
        # Deleted paths remain valid Git filters, but existing path components
        # must obey the same boundary as regular file reads.
        if is_sensitive_path(relative_scope):
            raise PathViolation("Credentials and Git internals are excluded from code tools.")
        current = self.workspace
        for part in Path(relative_scope).parts:
            current = current / part
            if current.is_symlink():
                raise PathViolation("Symlinks are not followed by code tools.")
            if not current.exists():
                break
        for root in (selected, *selected.parents):
            if not root.is_relative_to(self.workspace):
                break
            dot_git = root / ".git"
            if not dot_git.exists() and not dot_git.is_symlink():
                continue
            self._metadata_path(dot_git)
            if dot_git.is_dir():
                git_dir = dot_git
            elif dot_git.is_file():
                if dot_git.stat().st_size > 4096:
                    raise ValueError("Git directory pointer is too large.")
                raw = dot_git.read_text(encoding="utf-8").strip()
                if not raw.startswith("gitdir: ") or "\n" in raw or "\x00" in raw:
                    raise ValueError("Git directory pointer is invalid.")
                git_dir = self._metadata_path(root / raw[8:])
                if not git_dir.is_dir():
                    raise ValueError("Git directory pointer does not name an existing directory.")
            else:
                raise ValueError("Git metadata must be a regular directory or pointer file.")
            common = git_dir
            common_file = self._metadata_path(git_dir / "commondir")
            if common_file.exists():
                if not common_file.is_file() or common_file.stat().st_size > 4096:
                    raise ValueError("Git common directory pointer is invalid.")
                raw = common_file.read_text(encoding="utf-8").strip()
                if not raw or "\n" in raw or "\x00" in raw:
                    raise ValueError("Git common directory pointer is invalid.")
                common = self._metadata_path(git_dir / raw)
            # Git can otherwise read arbitrary object stores through alternates.
            for metadata in dict.fromkeys((git_dir, common)):
                for name in ("config", "config.worktree", "HEAD", "index", "packed-refs", "refs", "objects",
                             "objects/info", "objects/info/alternates", "objects/info/http-alternates"):
                    item = self._metadata_path(metadata / name)
                    if item.exists():
                        expected = stat.S_ISDIR if name in {"refs", "objects", "objects/info"} else stat.S_ISREG
                        if not expected(item.stat().st_mode):
                            raise PermissionError("Git metadata must use regular files and directories.")
                    if name.endswith("alternates") and item.exists() and item.stat().st_size:
                        raise PermissionError("Git alternate object stores are not supported in the work mount.")
                # Clean/process filters can execute during status and ordinary
                # diffs, even with --no-ext-diff/--no-textconv. They have no safe
                # role in this read-only service. Includes could load a host path.
                for config_name in ("config", "config.worktree"):
                    config = metadata / config_name
                    if config.exists():
                        if config.stat().st_size > 256_000:
                            raise ValueError("Git configuration exceeds the inspection bound.")
                        config_text = config.read_text(errors="replace").lstrip("\ufeff")
                        if re.search(r"(?im)^\s*\[\s*include(?:if)?(?:\s|\])", config_text):
                            raise PermissionError("Git configuration includes are not supported in the work mount.")
                        if re.search(r"(?im)^\s*\[\s*filter(?:\s|[.\]])", config_text):
                            raise PermissionError("Git filter configuration is unsupported by read-only repository tools.")
                # Ref and object subdirectories may themselves contain pointer
                # symlinks. Reject them without following links, under a fixed
                # metadata entry bound. Packed object files are not opened here.
                checked = 0
                for tree in (metadata / "refs", metadata / "objects"):
                    stack = [tree] if tree.is_dir() else []
                    while stack:
                        directory = stack.pop()
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                checked += 1
                                if checked > 200_000:
                                    raise ValueError("Git metadata exceeds the inspection bound; use a smaller checkout.")
                                if entry.is_symlink():
                                    raise PermissionError("Git metadata cannot contain symbolic links.")
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(Path(entry.path))
            return _Repository(root, git_dir, selected, str(scope.relative_to(root)))
        return None

    @staticmethod
    def _env() -> dict[str, str]:
        # Deliberately do not inherit the host's keys, Git helpers or configuration.
        return {
            "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat", "GIT_LITERAL_PATHSPECS": "1", "GIT_ATTR_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1", "GIT_PROTOCOL_FROM_USER": "0",
        }

    def _run(self, repo: _Repository, arguments: list[str], cap: int = _SCAN_BYTES) -> tuple[bytes, bool, int]:
        if not self.git:
            raise RuntimeError("Git is not installed in the code worker.")
        command = [self.git, "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
                   "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat", "-c", "color.ui=false",
                   "-c", "core.quotePath=false", "-c", "core.preloadIndex=false",
                   "-c", f"safe.directory={repo.root}", "-c", f"core.worktree={repo.root}",
                   f"--git-dir={repo.git_dir}", f"--work-tree={repo.root}", *arguments]
        process = subprocess.Popen(command, cwd=repo.root, env=self._env(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        selector = selectors.DefaultSelector()
        output = bytearray()
        stderr_size = 0
        truncated = False
        deadline = time.monotonic() + _TIMEOUT
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Git inspection exceeded its 12 second deadline. Narrow root or path.")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == "stdout":
                        room = cap - len(output)
                        output.extend(chunk[:max(room, 0)])
                        if len(chunk) > room:
                            truncated = True
                            return bytes(output), True, 0
                    else:
                        stderr_size += len(chunk)
                        if stderr_size > 65_536:
                            raise RuntimeError("Git inspection produced excessive diagnostics.")
            return bytes(output), truncated, process.wait(timeout=max(0.1, deadline - time.monotonic()))
        finally:
            selector.close()
            if process.poll() is None:
                try:
                    # These fixed read commands cannot launch hooks or helpers.
                    # Kill only the child we own, including under host test sandboxes.
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
            process.stderr.close()

    def _state(self, repo: _Repository) -> dict:
        head_raw, _, code = self._run(repo, ["rev-parse", "--verify", "HEAD^{commit}"], 1024)
        head = head_raw.decode("ascii", "replace").strip() if code == 0 else None
        branch_raw, _, code = self._run(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], 1024)
        branch = branch_raw.decode("utf-8", "replace").strip() if code == 0 else None
        return {"root": self.paths.relative(repo.root), "branch": branch, "head": head,
                "detached": head is not None and branch is None, "unborn": head is None}

    @staticmethod
    def _unavailable() -> dict:
        return {"available": False, "reason": "No Git repository exists in this scope inside the work mount.",
                "read_only": True}

    def _visible(self, repo: _Repository, name: str) -> str | None:
        relative = _relative(name)
        full = repo.root / relative
        value = str(full.relative_to(self.workspace))
        if is_sensitive_path(value):
            return None
        return value

    def context(self, args: dict | None = None) -> dict:
        args = args or {}
        repo = self._repository(args)
        if repo is None:
            return self._unavailable()
        state = self._state(repo)
        raw, scan_truncated, code = self._run(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                                    "--ignored=no", "--renames", "--ignore-submodules=all",
                                                    "--", repo.scope])
        if code:
            raise RuntimeError("Git could not inspect this repository's status.")
        entries = raw.split(b"\x00")
        rows = []
        counts = {"staged": 0, "unstaged": 0, "untracked": 0, "protected_omitted": 0}
        index = 0
        while index < len(entries) - 1:
            value = entries[index].decode("utf-8", "replace")
            index += 1
            if len(value) < 4:
                continue
            xy, name = value[:2], value[3:]
            previous = None
            if "R" in xy or "C" in xy:
                if index >= len(entries) - 1:
                    scan_truncated = True
                    break
                previous = entries[index].decode("utf-8", "replace")
                index += 1
            path = self._visible(repo, name)
            old_path = self._visible(repo, previous) if previous else None
            if path is None or previous and old_path is None:
                counts["protected_omitted"] += 1
                continue
            row = {"path": path, "status": xy, "staged": xy[0] not in (" ", "?"),
                   "unstaged": xy[1] not in (" ", "?"), "untracked": xy == "??"}
            if old_path:
                row["previous_path"] = old_path
            for flag in ("staged", "unstaged", "untracked"):
                counts[flag] += int(row[flag])
            rows.append(row)
        offset = _integer(args.get("offset"), 0, 0, 100_000, "offset")
        limit = _integer(args.get("limit"), 100, 1, 200, "limit")
        budget = _integer(args.get("max_bytes"), _OUTPUT_BYTES, 256, _OUTPUT_BYTES, "max_bytes")
        page = []
        used = 0
        for row in rows[offset:offset + limit]:
            size = len(str(row).encode("utf-8"))
            if used + size > budget:
                break
            page.append(row)
            used += size
        if not page and offset < len(rows):
            raise ValueError("max_bytes is too small for the next status row. Increase it.")
        active = self._active_files(args.get("active_files", []))
        after = self._state(repo)
        return {"available": True, "read_only": True, "repository": after,
                "scope": self.paths.relative(repo.root / repo.scope),
                "files": page, "counts": counts, "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
                "scan_truncated": scan_truncated, "counts_complete": not scan_truncated,
                "total_returnable": len(rows), "active_files": active,
                "freshness": {"checked_now": True, "head_changed_during_query": state != after,
                              "source": "live_git_status", "cached": False, "atomic_snapshot": False}}

    def _active_files(self, values: Any) -> list[dict]:
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError("active_files must contain at most 8 relative paths.")
        result = []
        for value in values:
            value = _relative(value, "active_files entry")
            if is_sensitive_path(value):
                raise PathViolation("Credentials and Git internals are excluded from code tools.")
            current = self.workspace
            for part in Path(value).parts:
                current = current / part
                if current.is_symlink():
                    raise PathViolation("Symlinks are not followed by code tools.")
                if not current.exists():
                    break
            try:
                data, info = self.paths.read_bytes(value, max_bytes=8 * 1024 * 1024)
                result.append({"path": value, "sha256": hashlib.sha256(data).hexdigest(),
                               "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                               "source": "current_workspace_file"})
            except PathViolation:
                if os.path.lexists(self.workspace / value):
                    raise
                result.append({"path": value, "missing": True, "source": "current_workspace_file"})
        return result

    def _commit(self, repo: _Repository, ref: Any) -> str:
        if not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref or "@{" in ref:
            raise ValueError("base must be a commit ID or a simple branch/tag revision such as HEAD~1.")
        raw, _, code = self._run(repo, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], 1024)
        value = raw.decode("ascii", "replace").strip()
        if code or not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise ValueError("base does not resolve to a commit in this repository.")
        return value

    def diff(self, args: dict | None = None) -> dict:
        args = args or {}
        repo = self._repository(args)
        if repo is None:
            return self._unavailable()
        state = self._state(repo)
        staged = args.get("staged", False)
        if not isinstance(staged, bool):
            raise ValueError("staged must be a boolean.")
        base = args.get("base", "HEAD")
        # An empty repository has a useful status but no HEAD diff yet.
        if state["unborn"] and base == "HEAD":
            return {"available": True, "read_only": True, "repository": state, "lines": [],
                    "reason": "This repository has no commit yet. Use repo_context to see staged and untracked paths.",
                    "next_offset": None, "scan_truncated": False}
        commit = self._commit(repo, base)
        command = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--unified=3",
                   "--ignore-submodules=all"]
        if staged:
            command.append("--cached")
        names_command = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                         "--ignore-submodules=all"]
        if staged:
            names_command.append("--cached")
        names_command.extend([commit, "--", repo.scope])
        names_raw, names_truncated, code = self._run(repo, names_command, 256_000)
        if code:
            raise RuntimeError("Git could not list this repository's changed paths.")
        names = []
        protected_omitted = 0
        for name_raw in names_raw.split(b"\x00")[:-1]:
            name = name_raw.decode("utf-8", "replace")
            if self._visible(repo, name) is not None:
                names.append(name)
            else:
                protected_omitted += 1
        if len(names) > 2000:
            names = names[:2000]
            names_truncated = True
        command.extend([commit, "--", *names])
        raw, scan_truncated, code = self._run(repo, command) if names else (b"", False, 0)
        scan_truncated |= names_truncated
        if code:
            raise RuntimeError("Git could not inspect this repository's diff.")
        visible_lines = raw.decode("utf-8", "replace").splitlines()
        offset = _integer(args.get("offset"), 0, 0, 100_000, "offset")
        limit = _integer(args.get("limit"), 200, 1, 300, "limit")
        budget = _integer(args.get("max_bytes"), _OUTPUT_BYTES, 256, _OUTPUT_BYTES, "max_bytes")
        page = []
        used = 0
        shortened = 0
        for index, value in enumerate(visible_lines[offset:offset + limit], start=offset):
            encoded = value.encode("utf-8")
            if len(encoded) > 4096:
                value = encoded[:4096].decode("utf-8", "ignore") + " [line truncated]"
                encoded = value.encode("utf-8")
                shortened += 1
            if used + len(encoded) + 32 > budget:
                break
            page.append({"line": index + 1, "text": value})
            used += len(encoded) + 32
        if not page and offset < len(visible_lines):
            raise ValueError("max_bytes is too small for the next diff line. Increase it.")
        after = self._state(repo)
        return {"available": True, "read_only": True, "repository": after, "base": commit, "staged": staged,
                "scope": self.paths.relative(repo.root / repo.scope), "lines": page, "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(visible_lines) else None,
                "total_returnable_lines": len(visible_lines), "scan_truncated": scan_truncated,
                "protected_omitted": protected_omitted, "shortened_lines": shortened,
                "note": "Untracked files are listed by repo_context and are not included in Git diffs.",
                "freshness": {"checked_now": True, "head_changed_during_query": state != after,
                              "source": "live_git_diff", "cached": False, "atomic_snapshot": False}}

    def history(self, args: dict | None = None) -> dict:
        args = args or {}
        repo = self._repository(args)
        if repo is None:
            return self._unavailable()
        state = self._state(repo)
        limit = _integer(args.get("limit"), 20, 1, 50, "limit")
        offset = _integer(args.get("offset"), 0, 0, 10_000, "offset")
        if state["unborn"]:
            rows = []
        else:
            raw, scan_truncated, code = self._run(repo, ["log", "--no-decorate", "--no-show-signature",
                                                        f"--skip={offset}", f"--max-count={limit + 1}",
                                                        "--format=%H%x00%aI%x00%s%x00", "--", repo.scope], 128_000)
            if code:
                raise RuntimeError("Git could not inspect this repository's history.")
            if scan_truncated:
                raise RuntimeError("Git history exceeded its output bound. Narrow root or path.")
            values = raw.decode("utf-8", "replace").split("\x00")
            rows = []
            for index in range(0, len(values) - 2, 3):
                rows.append({"commit": values[index].strip(), "authored_at": values[index + 1],
                             "subject": values[index + 2][:2048]})
        return {"available": True, "read_only": True, "repository": state,
                "scope": self.paths.relative(repo.root / repo.scope), "commits": rows[:limit], "offset": offset,
                "next_offset": offset + limit if len(rows) > limit else None,
                "freshness": {"checked_now": True, "source": "live_git_log", "cached": False}}
