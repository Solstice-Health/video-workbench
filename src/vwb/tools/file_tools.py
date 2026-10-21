"""File tools for agents, scoped to the request's work/ folder (Cursor-style editing).

Agents read, write and edit files here: canvas sources (outputs/<id>/canvas.json), notes,
drafts, advisor results. Nothing outside work/ is reachable; the copy deck, ledger,
renders and index live elsewhere and change only through their own tools.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any

from vwb.tools.base import Tool, ToolContext, ToolResult, schema

READ_BUDGET_CHARS = 48_000     # one read stays under the gateway's spill limit
READ_SCAN_BYTES = 8 * 1024**2
READ_LINE_BYTES = READ_BUDGET_CHARS * 4
LIST_SCAN_ENTRIES = 10_000
LIST_MAX_FILES = 500
SKIP_DIRECTORIES = {".sandbox", ".venv", "venv", "node_modules", "__pycache__", ".git"}


def _resolve(ctx: ToolContext, rel: str) -> Path:
    root = ctx.ws.work.resolve()
    p = (root / rel.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise PermissionError(f"{rel!r} is outside the work folder")
    return p


def _pattern(value: Any) -> str:
    pattern = value or "**/*"
    if (not isinstance(pattern, str) or len(pattern) > 512 or pattern.startswith("/")
            or "\\" in pattern or ".." in pattern.split("/") or "\x00" in pattern):
        raise ValueError("Use a relative work-folder glob without traversal.")
    return pattern


def _matches(path: str, pattern: str) -> bool:
    parts, tokens = path.split("/"), pattern.split("/")
    @cache
    def match(i: int, j: int) -> bool:
        if j == len(tokens):
            return i == len(parts)
        if tokens[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], tokens[j]) and match(i + 1, j + 1)
    return match(0, 0)


@contextmanager
def _directory(root: Path, parts: tuple[str, ...]):
    descriptors = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in parts:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _walk_files(ctx: ToolContext, pattern: str, counts: dict):
    root = ctx.ws.work
    if root.is_symlink():
        raise PermissionError("The work folder cannot be a symbolic link.")
    explicit = set(pattern.split("/"))
    counts.update(scanned_entries=0, truncated=False, excluded_directories=sorted(SKIP_DIRECTORIES - explicit))
    tokens = pattern.split("/")
    first_glob = next((index for index, token in enumerate(tokens) if any(char in token for char in "*?[")), len(tokens))
    prefix = tuple(tokens[:first_glob] if first_glob < len(tokens) else tokens[:-1])
    if len(prefix) >= 32:
        raise ValueError("The requested folder is deeper than the 32-level discovery bound.")
    stack = [prefix]
    while stack:
        parts = stack.pop()
        try:
            # Pin every directory before scanning, including a branch replaced
            # by an agent between discovery and traversal.
            with _directory(root, parts) as descriptor, os.scandir(descriptor) as entries:
                for entry in entries:
                    counts["scanned_entries"] += 1
                    if counts["scanned_entries"] > LIST_SCAN_ENTRIES:
                        counts["truncated"] = True
                        return
                    if entry.is_symlink():
                        continue
                    relative = (*parts, entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        if len(relative) >= 32:
                            counts["truncated"] = True
                        elif entry.name not in SKIP_DIRECTORIES or entry.name in explicit:
                            stack.append(relative)
                    elif entry.is_file(follow_symlinks=False) and _matches("/".join(relative), pattern):
                        yield root.joinpath(*relative), entry.stat(follow_symlinks=False).st_size
        except (FileNotFoundError, NotADirectoryError):
            continue  # A concurrent script may move a directory during discovery.



@contextmanager
def _read_stream(ctx: ToolContext, relative: str):
    """Pin each path component before reading mutable agent-owned files."""
    path = _resolve(ctx, relative)
    root = ctx.ws.work
    if root.is_symlink():
        raise PermissionError("The work folder cannot be a symbolic link.")
    parts = path.relative_to(root.resolve()).parts
    if not parts:
        raise ValueError("Choose a file, not the work directory.")
    descriptors = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if i < len(parts) - 1:
                flags |= os.O_DIRECTORY
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        if not stat.S_ISREG(os.fstat(descriptors[-1]).st_mode):
            raise ValueError("Only regular work-folder files can be read.")
        with os.fdopen(os.dup(descriptors[-1]), "rb") as stream:
            yield stream
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def file_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    pattern = _pattern(a.get("glob"))
    rows, counts = [], {}
    for path, size in _walk_files(ctx, pattern, counts):
        rows.append({"path": path.relative_to(ctx.ws.work).as_posix(), "bytes": size})
        if len(rows) >= LIST_MAX_FILES:
            counts["truncated"] = True
            break
    rows.sort(key=lambda row: row["path"])
    return ToolResult.json({"files": rows, **counts,
                            "note": "Use a narrower glob if truncated. Environment/cache folders are skipped unless explicitly named."})


def file_read(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    off, lim = a.get("offset", 0), a.get("limit", 600)
    if isinstance(off, bool) or not isinstance(off, int) or not 0 <= off <= 1_000_000:
        return ToolResult.error("offset must be an integer between 0 and 1,000,000 lines")
    if isinstance(lim, bool) or not isinstance(lim, int) or not 1 <= lim <= 2000:
        return ToolResult.error("limit must be an integer between 1 and 2,000 lines")
    out, scanned, chars, line_index = [], 0, 0, 0
    next_offset, reason = None, None
    try:
        with _read_stream(ctx, a["path"]) as stream:
            while line_index < off + lim:
                remaining = READ_SCAN_BYTES - scanned
                if remaining <= 0:
                    reason = "scan_limit"
                    break
                raw = stream.readline(min(READ_LINE_BYTES + 1, remaining))
                scanned += len(raw)
                if not raw:
                    break
                unfinished = not raw.endswith(b"\n") and bool(stream.peek(1))
                if unfinished:
                    reason = "long_line" if len(raw) >= READ_LINE_BYTES else "scan_limit"
                    if line_index >= off:
                        text = raw.decode("utf-8", errors="replace")[:max(0, READ_BUDGET_CHARS - chars)]
                        out.append(f"{line_index + 1:5d}\t{text}")
                    break
                if line_index < off:
                    line_index += 1
                    continue
                text = raw.decode("utf-8").rstrip("\r\n")
                if any(ord(c) < 32 and c not in "\t\f" for c in text):
                    return ToolResult.error("This file contains binary bytes; inspect it with attachment/PDF/image tools or the sandbox.")
                if len(text) + chars + 7 > READ_BUDGET_CHARS:
                    if out:
                        next_offset, reason = line_index, "output_limit"
                    else:
                        out.append(f"{line_index + 1:5d}\t{text[:READ_BUDGET_CHARS]}")
                        reason = "long_line"
                    break
                out.append(f"{line_index + 1:5d}\t{text}")
                chars += len(text) + 7
                line_index += 1
            else:
                if stream.peek(1):
                    next_offset, reason = line_index, "line_limit"
    except FileNotFoundError:
        return ToolResult.error(f"no file {a['path']}")
    except UnicodeDecodeError:
        return ToolResult.error("This file is not UTF-8 text; use the sandbox to inspect or convert its encoding.")
    note = ""
    if next_offset is not None:
        note = f"\n... More content remains; continue with offset={next_offset}."
    elif reason == "long_line":
        note = (f"\n... Line {line_index + 1} is too long for this view. "
                + ("Only its bounded prefix is shown. " if out else "The requested offset was not reached. ")
                + "Line pagination cannot resume inside it. Use sandbox byte-range reads.")
    elif reason == "scan_limit":
        note = "\n... Reached the 8 MiB scan limit before completing this page. Use sandbox streaming or byte-range reads; increasing the line offset does not bypass this bound."
    return ToolResult(text="\n".join(out) + note, data={"offset": off, "next_offset": next_offset,
                                                     "truncated": reason is not None, "reason": reason,
                                                     "scanned_bytes": scanned})


def file_write(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    p = _resolve(ctx, a["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a["content"])
    from vwb.util.hashing import sha256_text
    return ToolResult.json({"written": a["path"], "bytes": len(a["content"].encode())}, artifacts=[sha256_text(a["content"])])


def file_edit(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    p = _resolve(ctx, a["path"])
    if not p.is_file():
        return ToolResult.error(f"no file {a['path']}")
    if p.stat().st_size > READ_SCAN_BYTES:
        return ToolResult.error("This file exceeds the 8 MiB edit limit. Use a sandbox streaming script for large-file changes.")
    with _read_stream(ctx, a["path"]) as stream:
        raw = stream.read(READ_SCAN_BYTES + 1)
    if len(raw) > READ_SCAN_BYTES:
        return ToolResult.error("This file grew beyond the 8 MiB edit limit; use sandbox streaming.")
    text = raw.decode("utf-8")
    old, new = a["old"], a["new"]
    n = text.count(old)
    if n == 0:
        return ToolResult.error("old text not found (it must match exactly, including whitespace)")
    if n > 1 and not a.get("replace_all"):
        return ToolResult.error(f"old text occurs {n} times; add context to make it unique or set replace_all")
    text = text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1)
    p.write_text(text)
    from vwb.util.hashing import sha256_text
    return ToolResult.json({"edited": a["path"], "replacements": n if a.get("replace_all") else 1},
                           artifacts=[sha256_text(text)])


def file_grep(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    root = ctx.ws.work.resolve()
    rx = re.compile(a["pattern"])
    hits = []
    pattern = _pattern(a.get("glob") or "**/*")
    if "/" not in pattern:
        pattern = "**/" + pattern
    counts = {}
    for p, size in _walk_files(ctx, pattern, counts):
        if size > 2_000_000:
            continue
        with _read_stream(ctx, str(p.relative_to(root))) as stream:
            text = stream.read(2_000_001).decode("utf-8", errors="replace")
        if len(text) > 2_000_000:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append({"path": str(p.relative_to(root)), "line": i, "text": line[:240]})
                if len(hits) >= 200:
                    return ToolResult.json({"hits": hits, "truncated": True})
    return ToolResult.json({"hits": hits, **counts})


TOOLS = [
    Tool("file_list", "List up to 500 work-folder files. Skips .sandbox, venvs and caches unless explicitly named in the relative glob.",
         schema({"glob": {"type": "string"}}), file_list, "tool", "files"),
    Tool("file_read", "Read bounded UTF-8 text with line numbers (offset, limit in lines). Maximum scan 8 MiB; use sandbox streaming for giant files/lines.",
         schema({"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
         file_read, "tool", "files"),
    Tool("file_write", "Create or overwrite a file in the work folder.",
         schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
         file_write, "tool", "files"),
    Tool("file_edit", "Replace exact text in a file in the work folder (old must be unique unless replace_all).",
         schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                 "replace_all": {"type": "boolean"}}, ["path", "old", "new"]), file_edit, "tool", "files"),
    Tool("file_grep", "Search files in the work folder with a regular expression.",
         schema({"pattern": {"type": "string"}, "glob": {"type": "string"}}, ["pattern"]), file_grep, "tool", "files"),
]
