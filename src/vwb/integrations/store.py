"""Host-only integration state. Never mounted into an agent's work directory."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock


class IntegrationError(RuntimeError):
    """Safe, user-facing integration failure; never contains remote response bodies."""


class Store:
    def __init__(self, runs_dir: Path, namespace: str = "solstice"):
        if namespace not in {"solstice", "figma_direct", "figma_mcp", "blender_mcp", "mcp"}:
            raise IntegrationError("Unknown integration storage namespace.")
        self.root = Path(runs_dir) / ".integrations" / namespace
        # Do not follow a symlink planted by a request or another local process.
        for path in (Path(runs_dir), self.root.parent, self.root):
            if path.is_symlink():
                raise IntegrationError("Integration storage must not be a symlink.")
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root.parent, 0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "connection.json"
        self.lock_path = self.root / "connection.lock"
        if self.lock_path.is_symlink():
            raise IntegrationError("Integration lock must not be a symlink.")
        self.lock = FileLock(self.lock_path, timeout=45)

    def read(self) -> dict:
        if self.path.is_symlink():
            raise IntegrationError("Integration storage must not be a symlink.")
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text())
        except (ValueError, OSError):
            raise IntegrationError("Integration connection state cannot be read. Reconfigure the connection.") from None
        if not isinstance(value, dict):
            raise IntegrationError("Integration connection state is invalid. Reconfigure the connection.")
        return value

    def write(self, value: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=".connection-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
