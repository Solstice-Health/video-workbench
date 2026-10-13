"""Discover and bind explicit user-selected skills and agent-readable folders."""
from __future__ import annotations

import json
import os
import re
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from vwb.config import RECIPES_DIR

MAX_FOLDERS = 500
MAX_REFERENCES = 12
SKIP = {"node_modules", "__pycache__"}


def skills() -> list[dict[str, str]]:
    rows = []
    for path in sorted(RECIPES_DIR.glob("*/SKILL.md")):
        if path.is_symlink() or not re.fullmatch(r"[a-z0-9_-]+", path.parent.name):
            continue
        text = path.read_text()
        match = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
        fm = yaml.safe_load(match.group(1)) if match else {}
        fm = fm if isinstance(fm, dict) else {}
        rows.append({"id": path.parent.name, "name": str(fm.get("name", path.parent.name)),
                     "description": str(fm.get("description", ""))})
    return rows


def catalog(ws=None) -> dict[str, Any]:
    rows, pending = [], deque()
    if ws is not None and ws.work.is_dir() and not ws.work.is_symlink():
        rows.append({"id": ".", "name": "work/", "description": "All folders in this conversation's workspace"})
        pending.append((ws.work, 0))
    while pending and len(rows) < MAX_FOLDERS:
        directory, depth = pending.popleft()
        if depth >= 8:
            continue
        try:
            with os.scandir(directory) as entries:
                children = sorted((e for e in entries if not e.name.startswith(".") and e.name not in SKIP
                                   and e.is_dir(follow_symlinks=False)), key=lambda e: e.name.casefold())
        except OSError:
            continue
        for child in children:
            if len(rows) >= MAX_FOLDERS:
                break
            path = Path(child.path)
            rel = path.relative_to(ws.work).as_posix()
            rows.append({"id": rel, "name": f"work/{rel}/", "description": "Folder in this conversation"})
            pending.append((path, depth + 1))
    return {"skills": skills(), "folders": rows, "folder_scope": "conversation_work",
            "folders_truncated": bool(pending), "folder_limit": MAX_FOLDERS}


def validate(value: Any, ws=None) -> dict[str, list[str]]:
    if value is None:
        return {"skills": [], "folders": []}
    if not isinstance(value, dict) or set(value) - {"skills", "folders"}:
        raise ValueError("Context must contain selected skills and folders.")
    result = {}
    for kind in ("skills", "folders"):
        items = value.get(kind, [])
        if not isinstance(items, list) or any(not isinstance(v, str) or not v or len(v) > 512 for v in items):
            raise ValueError(f"Selected {kind} must be a list of identifiers.")
        result[kind] = list(dict.fromkeys(items))
    if sum(map(len, result.values())) > MAX_REFERENCES:
        raise ValueError(f"Choose at most {MAX_REFERENCES} references per message.")
    available = {s["id"] for s in skills()}
    if any(s not in available for s in result["skills"]):
        raise ValueError("A selected skill is no longer available. Remove it and choose again.")
    for rel in result["folders"]:
        if ws is None:
            raise ValueError("Choose a conversation before selecting a workspace folder.")
        parts = Path(rel).parts
        if Path(rel).is_absolute() or "\\" in rel or "\x00" in rel or any(
                p == ".." or p.startswith(".") or p in SKIP for p in parts):
            raise ValueError("That folder is outside the selectable workspace.")
        root = ws.work.resolve()
        path = ws.work
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("Symlink folders cannot be selected.")
        if ws.work.is_symlink() or not path.is_dir() or not path.resolve().is_relative_to(root):
            raise ValueError("A selected folder is missing or outside the workspace. Choose again.")
    return result


def prompt(value: dict[str, list[str]]) -> str:
    lines = []
    if value.get("skills"):
        lines.append("The user selected these skills. Read their procedures with skill_read(name) before using them: "
                     + json.dumps(value["skills"]))
    if value.get("folders"):
        lines.append("The user selected these folders, relative to your work/ directory. Inspect relevant files with "
                     "file_list/file_read; folder selection does not expand your permissions: " + json.dumps(value["folders"]))
    return "\n\nUser-selected references:\n" + "\n".join(lines) if lines else ""
