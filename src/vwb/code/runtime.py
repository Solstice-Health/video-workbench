"""Host client for the request-local, credential-free code service.

The private supervisor survives model turns, retaining a warm LSP session. Only
its read-only Docker worker sees workspace contents. No host language server or
project-supplied executable is used as a fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from filelock import FileLock
from filelock import Timeout as LockTimeout

IMAGE = "vwb-code-tools:1"
MAX_FRAME = 1024 * 1024
OPERATIONS = frozenset({"files", "search", "read", "context", "diff", "history", "lsp", "status"})


class CodeServiceError(RuntimeError):
    pass


def clean_environment() -> dict[str, str]:
    # Docker's host client may need its selected context. Nothing here is passed
    # into the container, which has a separate, fixed environment.
    names = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
    return {k: os.environ[k] for k in names if k in os.environ} | {"PYTHONUTF8": "1"}


def endpoint(workspace: Path) -> Path:
    root = Path(tempfile.gettempdir()) / f"vwb-code-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CodeServiceError("Code-service state directory must be private and owned by this user.")
    # A different checkout/version never reuses an older supervisor's code.
    digest = hashlib.sha256(str(workspace.resolve()).encode())
    for source in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(source.read_bytes())
    path = root / (digest.hexdigest()[:24] + ".sock")
    if len(os.fsencode(path)) >= 104:
        raise CodeServiceError("Temporary directory path is too long for the code-service socket.")
    return path


def send_frame(connection: socket.socket, value: dict) -> None:
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise CodeServiceError("Code-service message exceeds its byte limit; narrow the request.")
    connection.sendall(data)


def receive_frame(connection: socket.socket, *, deadline: float, cancel=None) -> dict:
    data = bytearray()
    connection.settimeout(0.2)
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Code lookup interrupted.")
        try:
            chunk = connection.recv(min(65536, MAX_FRAME + 1 - len(data)))
        except TimeoutError:
            continue
        if not chunk:
            raise CodeServiceError("Code service closed its connection. Retry the read-only lookup.")
        data.extend(chunk)
        if len(data) > MAX_FRAME:
            raise CodeServiceError("Code-service response exceeded its byte limit.")
        if b"\n" in chunk:
            result = json.loads(data.split(b"\n", 1)[0])
            if not isinstance(result, dict):
                raise CodeServiceError("Invalid code-service response.")
            return result
    raise CodeServiceError("Code service timed out. Narrow the lookup or retry.")


def _connect(path: Path) -> socket.socket:
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise CodeServiceError("Code-service socket is not private to this user.")
    connection = socket.socket(socket.AF_UNIX)
    connection.settimeout(0.5)
    try:
        connection.connect(str(path))
    except BaseException:
        connection.close()
        raise
    return connection


def _healthy(path: Path) -> bool:
    try:
        with _connect(path) as connection:
            send_frame(connection, {"operation": "ping"})
            return receive_frame(connection, deadline=time.monotonic() + 1).get("ready") is True
    except (OSError, ValueError, CodeServiceError):
        return False


def _ensure(workspace: Path, path: Path, cancel=None) -> None:
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Code lookup interrupted during service startup.")
    check_cancel()
    if _healthy(path):
        return
    lock = FileLock(str(path) + ".lock")
    deadline = time.monotonic() + 15
    while True:
        check_cancel()
        try:
            lock.acquire(timeout=0)
            break
        except LockTimeout:
            if time.monotonic() >= deadline:
                raise CodeServiceError("Code service is still starting in another process; retry shortly.") from None
            time.sleep(0.05)
    try:
        check_cancel()
        if _healthy(path):
            return
        if path.exists() or path.is_symlink():
            path.unlink()
        source_root = Path(__file__).resolve().parents[2]
        launcher = "import sys;sys.path.insert(0,sys.argv.pop(1));from vwb.code.service import main;main()"
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", launcher, str(source_root), str(workspace), str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=clean_environment(), cwd="/", start_new_session=True,
        )
        until = time.monotonic() + 8
        while time.monotonic() < until:
            check_cancel()
            if _healthy(path):
                return
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise CodeServiceError("Could not start the private code service. Check temporary-directory permissions.")
    finally:
        lock.release()


def invoke(workspace: Path, operation: str, arguments: dict | None = None, *, cancel=None) -> dict:
    if operation not in OPERATIONS:
        raise ValueError("Unsupported code-service operation.")
    if workspace.is_symlink() or not workspace.is_dir():
        raise CodeServiceError("Code tools require a regular conversation work folder.")
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Code lookup interrupted before execution.")
    path = endpoint(workspace)
    _ensure(workspace.resolve(), path, cancel=cancel)
    request_id = uuid.uuid4().hex
    with _connect(path) as connection:
        send_frame(connection, {"id": request_id, "operation": operation, "arguments": arguments or {}})
        try:
            response = receive_frame(connection, deadline=time.monotonic() + 45, cancel=cancel)
        except (InterruptedError, CodeServiceError):
            # Only this in-flight/queued lookup can be cancelled. It may restart
            # the shared read-only worker; other callers receive a retry error.
            try:
                with _connect(path) as control:
                    send_frame(control, {"operation": "cancel", "id": request_id})
            except OSError:
                pass
            raise
    if response.get("id") != request_id:
        raise CodeServiceError("Code-service response did not match this request.")
    if response.get("error"):
        raise CodeServiceError(str(response["error"]))
    return response["result"]


def stop(workspace: Path) -> bool:
    path = endpoint(workspace)
    if not _healthy(path):
        return False
    with _connect(path) as connection:
        send_frame(connection, {"operation": "shutdown"})
        receive_frame(connection, deadline=time.monotonic() + 5)
    return True


def image_status() -> dict:
    docker = shutil.which("docker")
    if not docker:
        return {"ready": False, "image": IMAGE, "message": "Install and start Docker, then run python -m vwb.code.runtime install."}
    try:
        result = subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True, timeout=8,
                                env=clean_environment(), check=False)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    return {"ready": result is not None and result.returncode == 0, "image": IMAGE,
            "message": "Fixed read-only code-tools image." if result is not None and result.returncode == 0 else
            "Start Docker and run python -m vwb.code.runtime install (no model calls)."}


def main() -> None:
    parser = argparse.ArgumentParser(description="Install/check the isolated repository tools; never starts a model turn.")
    parser.add_argument("command", choices=["install", "status", "stop"])
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(image_status()))
    elif args.command == "stop":
        if args.workspace is None:
            parser.error("stop requires --workspace")
        print(json.dumps({"stopped": stop(args.workspace)}))
    else:
        from vwb.config import RESOURCE_ROOT
        context = RESOURCE_ROOT / "docker" / "code-tools"
        docker = shutil.which("docker")
        if not docker or not (context / "Dockerfile").is_file():
            raise SystemExit("Docker or the packaged code-tools build files are missing.")
        subprocess.run([docker, "build", "-t", IMAGE, str(context)], env=clean_environment(), check=True)


if __name__ == "__main__":
    main()
