"""Project working files shared by conversations with separate private records.

Membership is host-owned metadata. No directory symlinks or model-controlled
absolute paths are used to extend the public workspace.
"""
from __future__ import annotations

import re
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock, Timeout

from vwb.util.hashing import sha256_file
from vwb.util.jsonio import read_json, write_json
from vwb.workspace.request import Workspace, now_iso

_ID = re.compile(r"p-[a-f0-9]{16}\Z")


class ProjectConflict(ValueError):
    pass


def _real_path(root: Path, path: Path) -> Path:
    cursor = root
    if root.is_symlink():
        raise ValueError("Project directories cannot use symbolic links.")
    for part in path.relative_to(root).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("Project directories cannot use symbolic links.")
    return path


def workspace_project(ws: Workspace) -> Path | None:
    marker = ws.root / ".project.json"
    if marker.is_symlink():
        raise ValueError("Invalid project membership.")
    data = read_json(marker)
    if data is None:
        return None
    pid = data.get("id")
    if not isinstance(pid, str) or not _ID.fullmatch(pid):
        raise ValueError("Invalid project identity.")
    root = Path(data.get("root", ""))
    if not root.is_absolute() or root.name != pid or root.parent.name != ".projects":
        raise ValueError("Invalid project workspace.")
    runs = root.parent.parent
    if not ws.root.resolve().is_relative_to(runs.resolve()):
        raise ValueError("The project belongs to another workbench.")
    _real_path(runs, root / "project.json")
    _real_path(runs, root / "work")
    if read_json(root / "project.json", {}).get("id") != pid or not (root / "work").is_dir():
        raise ValueError("The project workspace is unavailable.")
    return root


class ProjectStore:
    def __init__(self, runs_dir: Path):
        self.runs = Path(runs_dir).resolve()
        self.root = self.runs / ".projects"
        _real_path(self.runs, self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def folder(self, pid: str) -> Path:
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            raise ValueError("Invalid project identity.")
        return _real_path(self.runs, self.root / pid)

    def get(self, pid: str) -> dict:
        path = _real_path(self.runs, self.folder(pid) / "project.json")
        row = read_json(path)
        if not row or row.get("id") != pid:
            raise FileNotFoundError("That project was not found.")
        return row

    def list(self) -> list[dict]:
        return sorted((self.get(p.name) for p in self.root.iterdir()
                       if p.is_dir() and not p.is_symlink() and _ID.fullmatch(p.name)),
                      key=lambda row: (row["created"], row["id"]))

    def create(self, name: str, *, operation_id: str | None = None) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or any(ord(c) < 32 for c in name):
            raise ValueError("Name the project using 1–120 characters.")
        if operation_id is not None and (not isinstance(operation_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", operation_id)):
            raise ValueError("Invalid project operation identity.")
        pid = "p-" + (uuid.uuid5(uuid.NAMESPACE_URL, f"vwb-project:{operation_id}").hex[:16] if operation_id else uuid.uuid4().hex[:16])
        with FileLock(str(self.root / ".lock")):
            folder = self.folder(pid)
            if folder.exists():
                row = self.get(pid)
                if row["name"] != name.strip():
                    raise ProjectConflict("That operation already created a different project.")
                return row
            folder.mkdir()
            (folder / "work").mkdir()
            row = {"id": pid, "name": name.strip(), "created": now_iso()}
            write_json(folder / "project.json", row)
            return row

    @staticmethod
    def _merge_work(source: Path, target: Path) -> int:
        """Preflight all conflicts, then publish copies without overwriting files."""
        pending, directories = [], []
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if relative.parts[0] in {".codex", ".results"}:
                continue
            if path.is_symlink():
                raise ProjectConflict(f"Remove or replace the symbolic link {relative} before joining a project.")
            dest = _real_path(target, target / relative)
            if path.is_dir():
                if dest.exists() and not dest.is_dir():
                    raise ProjectConflict(f"Project file conflict: {relative}. Rename it or choose a new project.")
                directories.append(dest)
            elif path.is_file():
                if dest.exists():
                    if not dest.is_file() or sha256_file(path) != sha256_file(dest):
                        raise ProjectConflict(f"Project file conflict: {relative}. Rename it or choose a new project.")
                else:
                    pending.append((path, dest))
            else:
                raise ProjectConflict(f"Only regular files can join a project: {relative}.")
        created = []
        try:
            for folder in directories:
                folder.mkdir(parents=True, exist_ok=True)
            for source_file, dest in pending:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with source_file.open("rb") as incoming, dest.open("xb") as outgoing:
                    created.append(dest)
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                shutil.copystat(source_file, dest)
        except BaseException:
            for dest in reversed(created):
                dest.unlink(missing_ok=True)
            raise
        return len(pending)

    def move(self, ws: Workspace, pid: str) -> dict:
        row, target = self.get(pid), self.folder(pid)
        old = ws.project_root
        if old == target:
            return {"project": row, "copied_files": 0}
        locks = sorted({ws.root / "turn.lock", target / "turn.lock", *([old / "turn.lock"] if old else [])})
        try:
            with ExitStack() as stack:
                for lock in locks:
                    stack.enter_context(FileLock(str(lock), timeout=0))
                # Joining from a standalone conversation preserves relative names.
                # Moving between projects selects the destination workspace and
                # leaves the previous project's files available there.
                count = self._merge_work(ws.work, target / "work") if old is None else 0
                write_json(ws.root / ".project.json", {"id": pid, "root": str(target)})
                req = ws.load()
                req.project_id = pid
                ws.save(req)
                return {"project": row, "copied_files": count}
        except Timeout:
            raise ProjectConflict("Stop the active project turn before moving this conversation.") from None

    def bind_clone(self, source: Workspace, destination: Workspace) -> None:
        root = source.project_root
        if root:
            self.get(root.name)
            write_json(destination.root / ".project.json", {"id": root.name, "root": str(root)})
            req = destination.load()
            req.project_id = root.name
            destination.save(req)
