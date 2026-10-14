"""Explicit discovery of existing requests stored below the normal runs folder.

Registration keeps large assets and native conversation state in place. Only exact
local registrations are discoverable; this is not a recursive filesystem browser.
"""

from __future__ import annotations

import re
from pathlib import Path

from filelock import FileLock

from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z")


def _entries(runs_dir: Path) -> dict[str, str]:
    data = read_json(runs_dir / ".ui" / "history.json", {"schema": 1, "requests": {}})
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("requests"), dict):
        raise ValueError("Invalid conversation history registry.")
    return data["requests"]


def _root(runs_dir: Path, request_id: str, relative: str) -> Path:
    if not IDENTIFIER.fullmatch(request_id) or not isinstance(relative, str):
        raise ValueError("Invalid request registration.")
    path = Path(relative)
    if (not relative or "\\" in relative or "\x00" in relative or path.is_absolute()
            or any(part.startswith(".") for part in path.parts) or path.name != request_id):
        raise ValueError("Invalid request registration path.")
    root = runs_dir
    for part in path.parts:
        root = root / part
        if root.is_symlink():
            raise ValueError("Registered conversations cannot use symlink paths.")
    if not root.resolve().is_relative_to(runs_dir):
        raise ValueError("The conversation is outside this runs folder.")
    if (root / "request.json").is_symlink():
        raise ValueError("Registered conversations cannot use a symlink request record.")
    if not (root / "request.json").is_file():
        raise FileNotFoundError("That conversation was not found.")
    if Workspace(root).load().id != request_id:
        raise ValueError("The conversation id does not match its folder.")
    return root


def resolve_request(runs_dir: Path, request_id: str) -> Path:
    runs_dir = runs_dir.resolve()
    if not IDENTIFIER.fullmatch(request_id):
        raise ValueError("Invalid request id.")
    if (runs_dir / request_id / "request.json").exists():
        return _root(runs_dir, request_id, request_id)
    relative = _entries(runs_dir).get(request_id)
    if relative is None:
        raise FileNotFoundError("That conversation was not found.")
    return _root(runs_dir, request_id, relative)


def request_ids(runs_dir: Path) -> list[str]:
    runs_dir = runs_dir.resolve()
    found = {p.parent.name for p in runs_dir.glob("*/request.json")
             if IDENTIFIER.fullmatch(p.parent.name)}
    found.update(key for key in _entries(runs_dir) if isinstance(key, str) and IDENTIFIER.fullmatch(key))
    return sorted(found)


def register_request(runs_dir: Path, request_path: Path) -> str:
    """Register one existing, real request below runs_dir without changing it.

    This local administrative operation deliberately has no browser endpoint.
    """
    runs_dir = runs_dir.resolve()
    path = Path(request_path)
    if path.is_absolute():
        try:
            path = path.relative_to(runs_dir)
        except ValueError:
            raise ValueError("The conversation is outside this runs folder.") from None
    root = _root(runs_dir, path.name, path.as_posix())
    registry = runs_dir / ".ui" / "history.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(registry.with_suffix(".lock"))):
        entries = _entries(runs_dir)
        direct = runs_dir / root.name
        if (direct / "request.json").exists() and direct != root:
            raise ValueError("A different conversation already uses this id.")
        existing = entries.get(root.name)
        if existing is not None and existing != path.as_posix():
            raise ValueError("A different conversation is already registered with this id.")
        entries[root.name] = path.as_posix()
        write_json(registry, {"schema": 1, "requests": entries})
    return root.name
