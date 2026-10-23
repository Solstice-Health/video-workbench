"""Read-only, workspace-contained paths shared by the code intelligence worker."""

from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path, PurePath, PurePosixPath
from typing import BinaryIO

MAX_FILE_BYTES = 8 * 1024 * 1024
_PRIVATE_DIRS = {".git", ".aws", ".ssh", ".gnupg", ".azure", ".kube", ".secrets"}
_PRIVATE_NAMES = {
    "credentials", "credentials.json", "application_default_credentials.json",
    "secrets.json", "secrets.yaml", "secrets.yml", "token.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", "authorized_keys", "known_hosts",
}
_PRIVATE_PATTERNS = (".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*service-account*.json")


class PathViolation(ValueError):
    """A supplied path is outside the worker's public, read-only scope."""


def is_sensitive_path(relative: str | Path) -> bool:
    parts = PurePosixPath(str(relative).replace("\\", "/")).parts
    return any(
        part.lower() in _PRIVATE_DIRS or part.lower() in _PRIVATE_NAMES
        or any(fnmatch.fnmatchcase(part.lower(), pattern) for pattern in _PRIVATE_PATTERNS)
        for part in parts
    )


def signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class WorkspacePaths:
    """No absolute paths, traversal, symlink components, credentials or Git internals.

    Even an internal symlink is refused. The code worker has no need to follow
    links, and descriptor traversal also protects against swaps during a read.
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise PathViolation("The workspace must be a directory.")

    def _parts(self, value: str | Path = ".") -> tuple[str, ...]:
        if not isinstance(value, (str, PurePath)):
            raise PathViolation("Paths must be workspace-relative strings.")
        raw = str(value)
        if not raw or "\x00" in raw or "\\" in raw:
            raise PathViolation("Use a nonempty workspace-relative POSIX path.")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
            raise PathViolation("Absolute paths and parent traversal are not allowed.")
        if is_sensitive_path(path):
            raise PathViolation("Credentials and Git internals are excluded from code tools.")
        return tuple(part for part in path.parts if part != ".")

    def resolve(self, value: str | Path = ".", kind: str | None = None) -> Path:
        parts = self._parts(value)
        result = self.workspace
        try:
            for part in parts:
                result = result / part
                info = result.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise PathViolation("Symlinks are not followed by code tools.")
            info = result.stat()
        except (OSError, RuntimeError) as exc:
            raise PathViolation("The workspace path does not exist or cannot be read.") from exc
        if kind == "file" and not stat.S_ISREG(info.st_mode):
            raise PathViolation("The requested path is not a regular file.")
        if kind == "directory" and not stat.S_ISDIR(info.st_mode):
            raise PathViolation("The requested path is not a directory.")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise PathViolation("Only regular files and directories are supported.")
        return result

    def relative(self, path: Path) -> str:
        try:
            relative = Path(path).relative_to(self.workspace)
        except ValueError as exc:
            raise PathViolation("Path is outside the workspace.") from exc
        self._parts(relative)
        return relative.as_posix()

    def allowed(self, path: Path) -> bool:
        try:
            self.resolve(self.relative(path))
        except (PathViolation, OSError):
            return False
        return True

    def open_directory(self, value: str | Path = ".") -> int:
        parts = self._parts(value)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(self.workspace, flags)
        try:
            for part in parts:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except OSError as exc:
            os.close(descriptor)
            raise PathViolation("The workspace directory changed or is not readable.") from exc

    def open_file(self, value: str | Path) -> BinaryIO:
        parts = self._parts(value)
        if not parts:
            raise PathViolation("A regular file path is required.")
        parent = self.open_directory(PurePosixPath(*parts[:-1]) if len(parts) > 1 else ".")
        try:
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=parent,
            )
        except OSError as exc:
            raise PathViolation("The workspace file changed or is not readable.") from exc
        finally:
            os.close(parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise PathViolation("Only regular files can be read.")
        return os.fdopen(descriptor, "rb")

    def read_bytes(self, value: str | Path, max_bytes: int = MAX_FILE_BYTES) -> tuple[bytes, os.stat_result]:
        with self.open_file(value) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > max_bytes:
                raise PathViolation(f"File exceeds the {max_bytes}-byte code inspection limit.")
            raw = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > max_bytes:
            raise PathViolation(f"File exceeds the {max_bytes}-byte code inspection limit.")
        if signature(before) != signature(after):
            raise PathViolation("File changed during the read. Retry to obtain current evidence.")
        return raw, after
