"""File tools for agents, scoped to the request's work/ folder (Cursor-style editing).

Agents read, write and edit files here: canvas sources (outputs/<id>/canvas.json), notes,
drafts, advisor results. Nothing outside work/ is reachable; the copy deck, ledger,
renders and index live elsewhere and change only through their own tools.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from vwb.tools.base import Tool, ToolContext, ToolResult, schema

READ_BUDGET_CHARS = 48_000     # one read stays under the gateway's spill limit


def _resolve(ctx: ToolContext, rel: str) -> Path:
    root = ctx.ws.work.resolve()
    p = (root / rel.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise PermissionError(f"{rel!r} is outside the work folder")
    return p


def file_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    root = ctx.ws.work.resolve()
    pattern = a.get("glob") or "**/*"
    rows = [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size}
            for p in sorted(root.glob(pattern)) if p.is_file()]
    return ToolResult.json({"files": rows[:500]})


def file_read(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    p = _resolve(ctx, a["path"])
    if not p.is_file():
        return ToolResult.error(f"no file {a['path']}")
    lines = p.read_text().splitlines()
    off, lim = int(a.get("offset", 0)), int(a.get("limit", 600))
    out, size = [], 0
    for i, ln in enumerate(lines[off:off + lim], start=off):
        if size + len(ln) > READ_BUDGET_CHARS and out:
            break
        out.append(f"{i + 1:5d}\t{ln[:READ_BUDGET_CHARS]}")
        size += len(ln) + 7
    end = off + len(out)
    more = f"\n... {len(lines) - end} more line(s); continue with offset={end}" if len(lines) > end else ""
    return ToolResult(text="\n".join(out) + more)


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
    text = p.read_text()
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
    for p in sorted(root.rglob("*")):
        if not p.is_file() or not fnmatch.fnmatch(p.name, a.get("glob", "*")) or p.stat().st_size > 2_000_000:
            continue
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), start=1):
            if rx.search(line):
                hits.append({"path": str(p.relative_to(root)), "line": i, "text": line[:240]})
                if len(hits) >= 200:
                    return ToolResult.json({"hits": hits, "truncated": True})
    return ToolResult.json({"hits": hits})


TOOLS = [
    Tool("file_list", "List files in the request's work folder (glob relative to it).",
         schema({"glob": {"type": "string"}}), file_list, "tool", "files"),
    Tool("file_read", "Read a file in the work folder with line numbers (offset, limit in lines).",
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
