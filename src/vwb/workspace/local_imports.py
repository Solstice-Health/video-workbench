"""Explicit local file/folder snapshots, published once into the agent workspace.

Jobs run on the host, but never evaluate file content or grant agents access to
the source directory. Sources are pinned descriptors; work copies have separate
inodes, including when APFS can clone their blocks without copying the bytes.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from vwb.workspace.request import Workspace, now_iso

MAX_FILES = 10_000
MAX_ENTRIES = 50_000
MAX_BYTES = 100 * 1024**3
# imports/<id>/<selected name> consumes three levels of the tools' 32-level bound.
MAX_DEPTH = 29
MAX_WORK_PATH = 512
DISK_RESERVE = 16 * 1024**2
SKIP_DIRECTORIES = frozenset({"node_modules", "__pycache__", "venv", "env", "site-packages"})
TERMINAL = frozenset({"ready", "failed", "cancelled"})
_ID = re.compile(r"[a-f0-9]{32}\Z")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class _Cancelled(Exception):
    pass


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise _Cancelled()


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _valid_name(name: str) -> bool:
    return bool(name) and not name.startswith(".") and "\\" not in name and not any(
        ord(char) < 32 or ord(char) == 127 for char in name)


@contextmanager
def _child_directory(parent: int, name: str, *, create: bool = False, mode: int = 0o755):
    if create:
        try:
            os.mkdir(name, mode=mode, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _nested_directory(root: int, parts: tuple[str, ...], *, create: bool = False):
    current = os.dup(root)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current)
                except FileExistsError:
                    pass
            following = os.open(part, _DIRECTORY, dir_fd=current)
            os.close(current)
            current = following
        yield current
    finally:
        os.close(current)


def _same_directory(parent: int, name: str, descriptor: int) -> bool:
    try:
        actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
        expected = os.fstat(descriptor)
        return stat.S_ISDIR(actual.st_mode) and (
            actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
    except OSError:
        return False


def _json_write(directory: int, name: str, value: Any) -> None:
    temporary = f".{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise


def _json_read(directory: int, name: str) -> dict:
    descriptor = os.open(name, _READ, dir_fd=directory)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Import state must be a regular file.")
        return json.load(stream)


def _remove_tree(parent: int, name: str) -> None:
    """Remove only the named child; shutil's descriptor walk rejects symlink races."""
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


def _open_source(value: str, ws: Workspace) -> tuple[Path, int, bool]:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ValueError("Enter an absolute file or folder path, or a path starting with ~/ .")
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("Local paths cannot contain control characters or backslashes.")
    if not (value.startswith("/") or value.startswith("~/")):
        raise ValueError("Enter an absolute file or folder path, or a path starting with ~/ .")
    raw = value[2:] if value.startswith("~/") else value[1:]
    if any(part in {".", ".."} for part in raw.split("/")):
        raise ValueError("Use a direct local path without . or .. components.")
    path = Path(value).expanduser()
    home = Path.home()
    broad = {Path("/"), home, *home.parents, Path("/Volumes"), Path("/private"),
             Path("/private/tmp"), Path("/private/var"), Path("/System"), Path("/Library"),
             Path("/Applications"), Path("/usr"), Path("/opt")}
    if path in broad:
        raise ValueError("Choose a specific project or asset folder, not your home or a system root.")
    if any(not _valid_name(part) or part in SKIP_DIRECTORIES for part in path.parts[1:]):
        raise ValueError("Hidden paths and dependency folders cannot be imported. Choose an asset folder.")
    # Importing a parent of the request would scan our own growing staging tree.
    if ws.root.resolve().is_relative_to(path):
        raise ValueError("Choose a folder outside this request's parent directories.")
    descriptor = os.open("/", _DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            last = index == len(path.parts) - 2
            following = os.open(part, _READ if last else _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError("Choose a regular file or folder.")
        return path, descriptor, stat.S_ISDIR(mode)
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("Symbolic links cannot be imported. Choose the original file or folder.") from None
        if exc.errno == errno.ENOENT:
            raise ValueError("That local path was not found. Check the path and try again.") from None
        raise ValueError("That local path cannot be read. Check its permissions.") from None
    except BaseException:
        os.close(descriptor)
        raise


def _clone(source: int, destination: int, name: str) -> bool:
    """Clone from the already pinned source descriptor when APFS supports it."""
    if sys.platform != "darwin":
        return False
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "fclonefileat", None)
    if function is None:
        return False
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    function.restype = ctypes.c_int
    # CLONE_NOOWNERCOPY; target is one name under a pinned destination directory.
    if function(source, destination, os.fsencode(name), 2) == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOSYS}:
        return False
    raise OSError(error, os.strerror(error))


def _rename_new(source: int, name: str, destination: int, target: str) -> None:
    """Atomic no-replace publication, including a concurrent empty destination."""
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameatx_np" if sys.platform == "darwin" else "renameat2", None)
    if function is None:
        raise ValueError("This host cannot publish imports atomically without replacing existing files.")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    # Darwin RENAME_EXCL / Linux RENAME_NOREPLACE.
    flags = 4 if sys.platform == "darwin" else 1
    if function(source, os.fsencode(name), destination, os.fsencode(target), flags) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ValueError("The import destination already exists. Start a new import.")
        raise OSError(error, os.strerror(error))


def _copy_file(source: int, destination: int, name: str, expected: tuple[int, ...],
               cancel: threading.Event, progress) -> str:
    before = os.fstat(source)
    if not stat.S_ISREG(before.st_mode) or _stamp(before) != expected:
        raise ValueError("A source file changed after discovery. Start the import again.")
    _check(cancel)
    cloned = _clone(source, destination, name)
    if not cloned:
        available = os.fstatvfs(destination)
        if available.f_bavail * available.f_frsize < before.st_size + DISK_RESERVE:
            raise ValueError("There is not enough disk space to copy this import. Free space and try again.")
        target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=destination)
        with os.fdopen(target, "wb") as outgoing:
            copied = 0
            os.lseek(source, 0, os.SEEK_SET)
            while True:
                _check(cancel)
                block = os.read(source, min(1024**2, before.st_size - copied + 1))
                if not block:
                    break
                copied += len(block)
                if copied > before.st_size:
                    raise ValueError("A source file grew while it was being copied. Start the import again.")
                outgoing.write(block)
                progress(copied)
            if copied != before.st_size:
                raise ValueError("A source file changed while it was being copied. Start the import again.")
    _check(cancel)
    if _stamp(os.fstat(source)) != expected:
        raise ValueError("A source file changed while it was being copied. Start the import again.")
    target = os.open(name, _READ, dir_fd=destination)
    try:
        info = os.fstat(target)
        if not stat.S_ISREG(info.st_mode) or info.st_size != before.st_size or (
                info.st_dev, info.st_ino) == (before.st_dev, before.st_ino):
            raise ValueError("The imported file failed its independent-copy check.")
        os.fchmod(target, 0o644)
    finally:
        os.close(target)
    return "clone" if cloned else "copy"


class LocalImports:
    def __init__(self):
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vwb-import")
        self.owned: dict[tuple[str, str], threading.Event] = {}
        self.closed = False

    @staticmethod
    def _key(ws: Workspace, job_id: str) -> tuple[str, str]:
        return str(ws.root.resolve()), job_id

    @contextmanager
    def _job(self, ws: Workspace, job_id: str, *, create: bool = False):
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise ValueError("Invalid local import id.")
        root = os.open(ws.root, _DIRECTORY)
        try:
            with _child_directory(root, ".local-imports", create=create, mode=0o700) as store:
                with _child_directory(store, job_id, create=create, mode=0o700) as job:
                    yield job
        finally:
            os.close(root)

    @staticmethod
    def _public(row: dict) -> dict:
        return {key: value for key, value in row.items() if key not in {"source_path", "inventory"}}

    def _read(self, ws: Workspace, job_id: str) -> dict:
        with self._job(ws, job_id) as job:
            row = _json_read(job, "manifest.json")
            if row["status"] not in TERMINAL and self._key(ws, job_id) not in self.owned:
                _remove_tree(job, "stage")
                row.update(status="failed", ended=now_iso(),
                           error="The server stopped during this import. Start it again; nothing was resumed automatically.")
                _json_write(job, "manifest.json", row)
        return row

    def get(self, ws: Workspace, job_id: str) -> dict:
        with self.lock:
            return self._public(self._read(ws, job_id))

    def list(self, ws: Workspace) -> dict:
        with self.lock:
            root = os.open(ws.root, _DIRECTORY)
            try:
                try:
                    with _child_directory(root, ".local-imports") as store, os.scandir(store) as entries:
                        identities = [entry.name for entry in entries if _ID.fullmatch(entry.name)
                                      and entry.is_dir(follow_symlinks=False)]
                except FileNotFoundError:
                    identities = []
            finally:
                os.close(root)
            rows = [self._public(self._read(ws, identity)) for identity in identities]
            rows.sort(key=lambda row: row["created"], reverse=True)
            return {"jobs": rows}

    def start(self, ws: Workspace, path: str) -> dict:
        source_path, source, directory = _open_source(path, ws)
        submitted = False
        try:
            with self.lock:
                if self.closed:
                    raise ValueError("The local import service is shutting down.")
                root = self._key(ws, "")[0]
                if sum(key[0] == root for key in self.owned) >= 2:
                    raise ValueError("This conversation already has two imports in progress. Wait for one to finish.")
                identity = uuid.uuid4().hex
                row = {"id": identity, "status": "queued", "name": source_path.name,
                       "kind": "folder" if directory else "file", "files": 0, "total_files": 0,
                       "bytes": 0, "total_bytes": 0, "skipped": 0,
                       "skipped_details": {"hidden": 0, "dependency": 0, "symlink": 0, "unsupported": 0, "errors": 0},
                       "created": now_iso(), "source_path": str(source_path),
                       "limits": {"files": MAX_FILES, "entries": MAX_ENTRIES, "bytes": MAX_BYTES}}
                with self._job(ws, identity, create=True) as job:
                    _json_write(job, "manifest.json", row)
                cancel = threading.Event()
                self.owned[self._key(ws, identity)] = cancel
                try:
                    self.executor.submit(self._run, ws, row, source, directory, cancel)
                    submitted = True
                except BaseException:
                    self.owned.pop(self._key(ws, identity), None)
                    with self._job(ws, identity) as job:
                        row.update(status="failed", error="The import worker could not start.", ended=now_iso())
                        _json_write(job, "manifest.json", row)
                    raise
                return self._public(row.copy())
        finally:
            if not submitted:
                os.close(source)

    def cancel(self, ws: Workspace, job_id: str) -> dict:
        with self.lock:
            row = self._read(ws, job_id)
            if row["status"] not in TERMINAL:
                cancel = self.owned.get(self._key(ws, job_id))
                if cancel is not None:
                    cancel.set()
                row["cancel_requested"] = True
                with self._job(ws, job_id) as job:
                    _json_write(job, "manifest.json", row)
            return self._public(row)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for cancel in self.owned.values():
                cancel.set()
        self.executor.shutdown(wait=True)

    def _scan(self, source: int, directory: bool, row: dict, cancel: threading.Event, progress) -> list[dict]:
        inventory = []
        seen = 0
        pending = [()] if directory else []

        def add(parts: tuple[str, ...], info: os.stat_result):
            if len(inventory) >= MAX_FILES:
                raise ValueError(f"This folder exceeds the {MAX_FILES:,}-file import limit. Choose a smaller folder.")
            if row["total_bytes"] + info.st_size > MAX_BYTES:
                raise ValueError(f"This import exceeds the {MAX_BYTES / 1024**3:g} GiB size limit. Choose a smaller batch.")
            work_path = f"imports/{row['id']}/{row['name']}" + ("/" + "/".join(parts) if parts else "")
            if len(work_path.encode()) > MAX_WORK_PATH:
                raise ValueError(f"An imported workspace path exceeds {MAX_WORK_PATH} bytes. Shorten its folder or file names.")
            inventory.append({"path": "/".join(parts), "bytes": info.st_size, "stamp": _stamp(info)})
            row["total_files"] += 1
            row["total_bytes"] += info.st_size

        if not directory:
            add((), os.fstat(source))
        while pending:
            _check(cancel)
            parts = pending.pop()
            with _nested_directory(source, parts) as folder, os.scandir(folder) as entries:
                for entry in entries:
                    _check(cancel)
                    seen += 1
                    if seen > MAX_ENTRIES:
                        raise ValueError(f"This folder exceeds the {MAX_ENTRIES:,}-entry scan limit. Choose a smaller folder.")
                    reason = None
                    if not _valid_name(entry.name):
                        reason = "hidden"
                    elif entry.name in SKIP_DIRECTORIES and entry.is_dir(follow_symlinks=False):
                        reason = "dependency"
                    elif entry.is_symlink():
                        reason = "symlink"
                    else:
                        info = entry.stat(follow_symlinks=False)
                        child = (*parts, entry.name)
                        if len(child) > MAX_DEPTH:
                            raise ValueError(f"This folder exceeds the {MAX_DEPTH}-level depth limit. Choose a shallower folder.")
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(child)
                        elif stat.S_ISREG(info.st_mode):
                            add(child, info)
                        else:
                            reason = "unsupported"
                    if reason:
                        row["skipped"] += 1
                        row["skipped_details"][reason] += 1
                    progress()
        if not inventory:
            raise ValueError("This folder contains no importable regular files. Hidden files, links and dependency folders are skipped.")
        return inventory

    def _run(self, ws: Workspace, row: dict, source: int, directory: bool, cancel: threading.Event) -> None:
        identity = row["id"]
        try:
            with self._job(ws, identity) as job:
                last_write = 0.0

                def progress(force=False):
                    nonlocal last_write
                    now = time.monotonic()
                    if force or now - last_write >= 0.25:
                        with self.lock:
                            _json_write(job, "manifest.json", row)
                        last_write = now

                try:
                    _check(cancel)
                    row.update(status="running", phase="discovering")
                    progress(True)
                    inventory = self._scan(source, directory, row, cancel, progress)
                    _json_write(job, "files.json", {"files": inventory})
                    row.update(phase="copying", inventory="files.json")
                    progress(True)
                    os.mkdir("stage", mode=0o755, dir_fd=job)
                    with _child_directory(job, "stage") as stage:
                        for item in inventory:
                            _check(cancel)
                            relative = tuple(item["path"].split("/")) if item["path"] else ()
                            dest_parts = (row["name"], *relative) if directory else (row["name"],)
                            completed_bytes = row["bytes"]

                            def byte_progress(copied, completed=completed_bytes):
                                row["bytes"] = completed + copied
                                progress()

                            with _nested_directory(stage, dest_parts[:-1], create=True) as destination:
                                if directory:
                                    with _nested_directory(source, relative[:-1]) as parent:
                                        incoming = os.open(relative[-1], _READ, dir_fd=parent)
                                else:
                                    incoming = os.dup(source)
                                try:
                                    _copy_file(incoming, destination, dest_parts[-1], tuple(item["stamp"]),
                                               cancel, byte_progress)
                                finally:
                                    os.close(incoming)
                            row["files"] += 1
                            row["bytes"] = completed_bytes + item["bytes"]
                            progress()
                    _check(cancel)
                    reference_id = f"imports/{identity}/{row['name']}"

                    def ready():
                        row.update(status="ready", phase="complete", ended=now_iso(),
                                   reference={"kind": row["kind"], "id": reference_id,
                                              "name": f"work/{reference_id}" + ("/" if directory else ""),
                                              "description": f"Imported {row['files']:,} file" + ("s" if row["files"] != 1 else "")})
                        progress(True)

                    self._publish(ws, job, identity, cancel, ready)
                except _Cancelled:
                    _remove_tree(job, "stage")
                    row.update(status="cancelled", ended=now_iso(), error="The import was cancelled. No files were published.")
                    progress(True)
                except Exception as exc:
                    _remove_tree(job, "stage")
                    row.pop("reference", None)
                    row["skipped_details"]["errors"] += 1
                    message = str(exc) if isinstance(exc, ValueError) else (
                        "There is not enough disk space for this import." if isinstance(exc, OSError) and exc.errno == errno.ENOSPC
                        else "The import could not read or publish a file. Check that the source and workspace are accessible, then try again.")
                    row.update(status="failed", ended=now_iso(), error=message)
                    progress(True)
        finally:
            os.close(source)
            with self.lock:
                self.owned.pop(self._key(ws, identity), None)

    def _publish(self, ws: Workspace, job: int, identity: str, cancel: threading.Event, ready) -> None:
        root = os.open(ws.root, _DIRECTORY)
        try:
            with _child_directory(root, "work") as work:
                with _child_directory(work, "imports", create=True) as imports:
                    _check(cancel)
                    if not _same_directory(root, "work", work) or not _same_directory(work, "imports", imports):
                        raise ValueError("The workspace folder changed during import. Try again.")
                    with self.lock:
                        _check(cancel)
                        _rename_new(job, "stage", imports, identity)
                        try:
                            if not _same_directory(root, "work", work) or not _same_directory(work, "imports", imports):
                                raise ValueError("The workspace folder changed during import. Try again.")
                            ready()
                        except BaseException:
                            _remove_tree(imports, identity)
                            raise
        finally:
            os.close(root)
