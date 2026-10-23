"""Fixed stdio RPC worker inside the read-only, network-free code container."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from .fence import HostFence
from .git_context import GitContext
from .lsp import LspPool
from .search import SearchEngine

MAX_FRAME = 1024 * 1024


class CodeWorker:
    def __init__(self, workspace: Path):
        self.search = SearchEngine(workspace)
        self.git = GitContext(workspace)
        self.lsp = LspPool(workspace)
        self.fence = HostFence(workspace)
        self.instance = uuid.uuid4().hex

    def call(self, operation: str, args: dict) -> dict:
        handlers = {"files": self.search.files, "search": self.search.search, "read": self.search.read,
                    "context": self.git.context, "diff": self.git.diff, "history": self.git.history,
                    "lsp": self.lsp.call}
        if operation == "status":
            return {"ready": True, "lsp": self.lsp.status(), "worker_pid": os.getpid(), "worker_instance": self.instance,
                    "workspace": "/workspace", "read_only": True, "network": False}
        if operation not in handlers:
            raise ValueError("Unsupported code operation.")
        return handlers[operation](args)

    def close(self) -> None:
        self.lsp.close()


def main() -> None:
    if not Path("/.dockerenv").exists():
        raise SystemExit("The code worker runs only in its fixed Docker image.")
    worker = CodeWorker(Path("/workspace"))
    try:
        while line := sys.stdin.buffer.readline(MAX_FRAME + 1):
            if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                break
            request = {}
            try:
                request = json.loads(line)
                args = request.get("arguments", {})
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                if not worker.fence.wait(request):
                    raise ValueError("Workspace mount has not caught up with host file hashes. Retry this read-only lookup.")
                worker.search.accept_host_evidence(request)
                if request["operation"] == "lsp":
                    worker.lsp.accept_host_evidence(request)
                result = {"id": request["id"], "result": worker.call(request["operation"], args)}
            except Exception as exc:
                result = {"id": request.get("id"), "error": f"{type(exc).__name__}: {str(exc)[:2000]}"}
            data = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
            if len(data) > MAX_FRAME:
                data = json.dumps({"id": request.get("id"), "error": "Code result exceeded its byte limit; narrow the lookup."}).encode() + b"\n"
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    finally:
        worker.close()


if __name__ == "__main__":
    main()
