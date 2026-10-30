"""Discover and bind explicit user-selected skills and agent-readable paths."""
from __future__ import annotations

import json
import os
import re
import stat
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from vwb.config import RECIPES_DIR

MAX_FOLDERS = 500
MAX_REFERENCES = 100
MAX_RESULTS = 100
MAX_SCAN = 10_000
MAX_DEPTH = 32
SKIP = {"node_modules", "__pycache__", "venv", "site-packages"}
PLUGINS = {"solstice", "figma", "figma_direct", "figma_mcp", "blender_mcp", "after_effects"}


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


def _parts(rel: str) -> tuple[str, ...]:
    parts = Path(rel).parts
    if (Path(rel).is_absolute() or "\\" in rel or any(ord(c) < 32 or ord(c) == 127 for c in rel)
            or any(p == ".." or p.startswith(".") or p in SKIP for p in parts)):
        raise ValueError("That path is outside the selectable workspace.")
    return parts


def _directory(root: Path, parts: tuple[str, ...] = ()) -> int:
    """Pin each parent, so a swapped directory cannot redirect discovery."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_path(ws, rel: str, *, folder: bool) -> None:
    parts = _parts(rel)
    if not parts and not folder:
        raise ValueError("Choose a file, not the workspace root.")
    fd = _directory(ws.work, parts if folder else parts[:-1])
    try:
        if not folder and not stat.S_ISREG(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode):
            raise ValueError("Select a regular file; symbolic links are not supported.")
    finally:
        os.close(fd)


@contextmanager
def open_file(ws, rel: str):
    """Open a selected regular file through pinned, no-follow work descriptors."""
    if not isinstance(rel, str) or not rel or len(rel) > 512:
        raise ValueError("Choose a file relative to work/.")
    parts = _parts(rel)
    if not parts:
        raise ValueError("Choose a file, not the workspace root.")
    parent = _directory(ws.work, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Select a regular file; symbolic links are not supported.")
        yield stream


def catalog(ws=None, query: str = "") -> dict[str, Any]:
    if not isinstance(query, str) or len(query) > 512 or any(ord(c) < 32 for c in query):
        raise ValueError("Search workspace paths with at most 512 characters.")
    query = query.strip()
    # Absolute paths inside work/ are searchable; other host paths are only
    # opened by the explicit local-import operation, never by autocomplete.
    if ws is not None and query.startswith(str(ws.work) + "/"):
        query = query[len(str(ws.work)) + 1:]
    elif query == "work":
        query = ""
    elif query.startswith("work/"):
        query = query[5:]
    rows, pending = [], deque()
    scanned, truncated = 0, False
    needle = query.rstrip("/").casefold()
    if ws is not None and not query.startswith(("/", "~")):
        try:
            fd = _directory(ws.work)
            os.close(fd)
            start = ()
            exact_file = False
            if query:
                try:
                    _check_path(ws, query, folder=False)
                    rel = "/".join(_parts(query))
                    rows.append({"id": rel, "name": f"work/{rel}", "kind": "file",
                                 "description": "File in this conversation's workspace"})
                    exact_file = True
                except (OSError, ValueError):
                    pass
            # A path prefix narrows traversal before scanning, so a folder past
            # the discovery budget can still be reached directly.
            parent = query.rstrip("/") if query.endswith("/") else query.rpartition("/")[0]
            for prefix in dict.fromkeys([query.rstrip("/"), parent]):
                if not prefix or exact_file:
                    continue
                try:
                    candidate = _parts(prefix)
                    fd = _directory(ws.work, candidate)
                    os.close(fd)
                    start = candidate
                    break
                except (OSError, ValueError):
                    pass
            rel = "/".join(start) or "."
            if not exact_file and (not needle or needle in rel.casefold()):
                rows.append({"id": rel, "name": f"work/{'/'.join(start)}" + ("/" if start else ""),
                             "description": "All files in this folder, read as needed", "kind": "folder"})
            if not exact_file:
                pending.append(start)
        except OSError:
            pass
    while pending and len(rows) < MAX_RESULTS and scanned < MAX_SCAN:
        parts = pending.popleft()
        if len(parts) >= MAX_DEPTH:
            truncated = True
            continue
        try:
            fd = _directory(ws.work, parts)
            try:
                with os.scandir(fd) as entries:
                    children = []
                    for entry in entries:
                        if scanned >= MAX_SCAN:
                            truncated = True
                            break
                        scanned += 1
                        if entry.name.startswith(".") or entry.name in SKIP:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            children.append((entry.name, "folder"))
                        elif entry.is_file(follow_symlinks=False):
                            children.append((entry.name, "file"))
            finally:
                os.close(fd)
        except OSError:
            truncated = True
            continue
        for name, kind in sorted(children, key=lambda item: (item[1] != "folder", item[0].casefold())):
            child_parts = (*parts, name)
            rel = "/".join(child_parts)
            if kind == "folder":
                pending.append(child_parts)
            if not needle or needle in rel.casefold():
                if len(rows) >= MAX_RESULTS:
                    truncated = True
                    break
                rows.append({"id": rel, "name": f"work/{rel}" + ("/" if kind == "folder" else ""),
                             "description": ("All files in this folder, read as needed" if kind == "folder"
                                             else "File in this conversation's workspace"), "kind": kind})
    truncated |= bool(pending)
    from vwb.workflows import CATALOG
    return {"skills": skills(), "workflows": CATALOG, "folders": [r for r in rows if r["kind"] == "folder"],
            "files": [r for r in rows if r["kind"] == "file"], "folder_scope": "conversation_work",
            "folders_truncated": truncated, "paths_truncated": truncated, "scanned": scanned,
            "path_limit": MAX_RESULTS, "folder_limit": MAX_FOLDERS, "max_references": MAX_REFERENCES,
            "search_query": query}


def validate(value: Any, ws=None) -> dict[str, list[str]]:
    if value is None:
        return {"skills": [], "folders": []}
    if not isinstance(value, dict) or set(value) - {"skills", "folders", "files", "plugins", "workflows"}:
        raise ValueError("Context must contain selected skills, folders, files and plugins.")
    result = {}
    for kind in ("skills", "folders", "files", "plugins", "workflows"):
        items = value.get(kind, [])
        if not isinstance(items, list) or any(not isinstance(v, str) or not v or len(v) > 512 for v in items):
            raise ValueError(f"Selected {kind} must be a list of identifiers.")
        result[kind] = list(dict.fromkeys(items))
    if any(plugin not in PLUGINS and not re.fullmatch(r"mcp_[a-f0-9]{12}", plugin) for plugin in result["plugins"]):
        raise ValueError("A selected plugin is unavailable. Remove it and choose again.")
    # Preserve the canonical form of older messages and queue records. A plugin
    # selection is a request to use that integration, never an OAuth credential.
    if not result["plugins"]:
        result.pop("plugins")
    if not result["files"]:
        result.pop("files")
    if not result["workflows"]:
        result.pop("workflows")
    from vwb.workflows import validate_selection
    validate_selection(ws, result)
    if sum(map(len, result.values())) > MAX_REFERENCES:
        raise ValueError(f"Choose at most {MAX_REFERENCES} references per message.")
    available = {s["id"] for s in skills()}
    if any(s not in available for s in result["skills"]):
        raise ValueError("A selected skill is no longer available. Remove it and choose again.")
    for kind in ("folders", "files"):
        for rel in result.get(kind, []):
            if ws is None:
                raise ValueError("Choose a conversation before selecting a workspace path.")
            try:
                _check_path(ws, rel, folder=kind == "folders")
            except OSError as exc:
                raise ValueError("A selected path is missing or outside the workspace. Choose again.") from exc
    return result


def prompt(value: dict[str, list[str]]) -> str:
    lines = []
    if value.get("skills"):
        lines.append("The user selected these skills. Read their procedures with skill_read(name) before using them: "
                     + json.dumps(value["skills"]))
    if value.get("folders"):
        lines.append("The user selected these folders, relative to your work/ directory, including their nested files: "
                     + json.dumps(value["folders"]) + ". This is a batch reference, not an inline attachment limit. "
                     "Enumerate the requested inputs with file_list using a scoped glob, or a sandbox script for large batches. "
                     "Check truncation and account for every requested input. Read files, PDF pages or image pixels only as needed "
                     "with the appropriate inspection tools. Imported videos are work files; they are not automatically indexed "
                     "media sources. Use media_register(path) for a video you need as a source, and index_build(media_id) "
                     "to index it. Use sandbox tools for bulk transformations. Folder selection does not expand your permissions.")
    if value.get("files"):
        lines.append("The user selected these files, relative to your work/ directory: " + json.dumps(value["files"])
                     + ". Inspect them with file_read, image/PDF tools or a sandbox parser as appropriate. "
                     "Selected video files are snapshotted as immutable source refs before this turn; use the registered "
                     "media refs and index tools below. Other files remain live references, without automatically loaded "
                     "contents or pinned image payloads.")
    if value.get("plugins"):
        lines.append("The user selected these plugins or MCP connections: " + json.dumps(value["plugins"])
                     + ". Inspect integration_status before using their tools. For saved mcp_* connections, pass the exact "
                     "id to integration_tools, then use each tool's dispatch_tool and namespaced name. These servers "
                     "connect directly and do not need Solstice. Use integration='figma_direct' for the "
                     "separate Figma direct REST connection, integration='figma_mcp' for official desktop MCP, or integration='figma' for Figma through the Solstice MCP gateway. Respect the selected "
                     "route; never silently switch connections or assume a disconnected integration is available. "
                     "Import retrieved previews into work/integrations so the user can open them in the workspace canvas. "
                     "Selection does not grant additional permissions or authorize unrelated writes.")
        if "after_effects" in value["plugins"]:
            lines.append("After Effects uses native after_effects_status, after_effects_run, after_effects_render "
                         "and after_effects_job tools. Write ExtendScript .jsx files under work/ and use the returned "
                         "host workspace path for imports and outputs. Local automation must be enabled by the person "
                         "in Manage connections; never bypass that switch through a shell or sandbox. These scripts "
                         "run in the real desktop application, not in Docker. Preserve an existing unsaved project.")
        if "blender_mcp" in value["plugins"]:
            lines.append("Use integration_tools(integration='blender_mcp') for the pinned local Blender MCP bridge. "
                         "Use integration_call for inspection and integration_execute for Python only after the person "
                         "enables code execution. Save .blend files and rendered media under the returned workspace "
                         "directory; inspect receipts and files before claiming output success. Blender Python runs "
                         "in the real application with the person's local account access.")
    return "\n\nUser-selected references:\n" + "\n".join(lines) if lines else ""
