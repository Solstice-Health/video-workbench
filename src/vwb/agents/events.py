"""Public agent activity for the workbench, separate from the audit ledger.

Only readable provider output is exposed. Encrypted reasoning, image bytes and credentials
are never copied to the feed. Token counters distinguish the latest context measurement
from cumulative gateway usage; semantic token categories are not inferred.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from vwb.util.jsonio import append_jsonl, read_json, write_json
from vwb.workspace import Workspace

EVENT_FILE = "chat_events.jsonl"
MAX_TOOL_CHARS = 24000
_context_cache: dict[str, dict[str, Any]] = {}
_context_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _rows(path: Path):
    if not path.exists():
        return
    with path.open() as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue  # another process can be part-way through its last append
            if isinstance(row, dict):
                yield row


def _last_seq(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        # Every append ends in a newline. Read backwards until the last complete row fits.
        n = min(size, 65536)
        while n:
            fh.seek(size - n)
            lines = fh.read().splitlines()
            for line in reversed(lines[1:] if n < size else lines):
                try:
                    return int(json.loads(line).get("seq", 0))
                except (ValueError, TypeError):
                    continue
            if n == size:
                break
            n = min(size, n * 2)
    return 0


def emit(ws: Workspace, kind: str, data: dict[str, Any] | None = None, *, actor: str = "job_lead") -> dict[str, Any]:
    _ensure_history(ws)
    path = ws.memory / EVENT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        row = {"seq": _last_seq(path) + 1, "ts": _now(), "kind": kind, "actor": actor, "data": data or {}}
        append_jsonl(path, row)
    return row


def read_events(ws: Workspace, after: int = 0) -> list[dict[str, Any]]:
    _ensure_history(ws)
    return [r for r in _rows(ws.memory / EVENT_FILE) if int(r.get("seq", 0)) > after]


def _public_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict)
                         and p.get("type") in ("text", "input_text", "output_text", "summary_text"))
    return ""


def _ensure_history(ws: Workspace) -> None:
    """Once, import timestamped public items from older request-local Codex rollouts.

    Exec transcripts lack timestamps. Rollouts have timestamps and explicit public text fields;
    system/developer instructions and opaque reasoning payloads are deliberately excluded.
    """
    path = ws.memory / EVENT_FILE
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        if path.exists():
            return
        rows = []
        for rollout in sorted((ws.root / "agent" / "codex" / "sessions").glob("**/*.jsonl")):
            calls = {}
            for row in _rows(rollout):
                if row.get("type") != "response_item":
                    continue
                payload = row.get("payload") or {}
                typ = payload.get("type")
                kind, data = None, {"source": "codex", "status": "completed", "historical": True}
                ident = payload.get("call_id") or payload.get("id") or row.get("ordinal", len(rows))
                data["id"] = f"history:{rollout.stem}:{ident}"
                if typ == "message" and payload.get("role") == "assistant":
                    text = _public_text(payload.get("content"))
                    if text:
                        kind, data = "message", {**data, "text": text, "phase": payload.get("phase") or payload.get("channel")}
                elif typ == "reasoning":
                    text = _public_text(payload.get("summary"))
                    if text:
                        kind, data = "thinking", {**data, "text": text}
                elif typ in ("function_call", "custom_tool_call"):
                    args = payload.get("arguments", payload.get("input", ""))
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"code": args[:MAX_TOOL_CHARS]}
                    data.update(name=payload.get("name", "tool"), arguments=args, status="in_progress")
                    calls[ident] = dict(data)
                    kind = "tool"
                elif typ in ("function_call_output", "custom_tool_call_output") and ident in calls:
                    kind, data = "tool", {**calls[ident], "status": "completed",
                                          "output": _public_text(payload.get("output"))[:MAX_TOOL_CHARS]}
                if kind:
                    rows.append({"ts": row.get("timestamp") or _now(), "kind": kind, "actor": "job_lead", "data": data})
        rows.sort(key=lambda r: r["ts"])
        path.touch()
        for seq, row in enumerate(rows, 1):
            append_jsonl(path, {"seq": seq, **row})


def _tool_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result or "")[:MAX_TOOL_CHARS]
    texts = [str(c.get("text", "")) for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(texts)[:MAX_TOOL_CHARS]


def codex_event(ws: Workspace, ev: dict[str, Any], ticket: str) -> None:
    """Normalize Codex exec JSON item snapshots. A stable id lets clients replace updates."""
    kind = ev.get("type", "")
    if kind == "thread.started":
        emit(ws, "thread.started", {"source": "codex", "thread_id": ev.get("thread_id"), "attempt": ticket})
        return
    if kind == "turn.completed":
        emit(ws, "usage", {"source": "codex", "scope": "turn", "usage": ev.get("usage") or {}, "attempt": ticket})
        return
    if kind in ("turn.failed", "error"):
        error = ev.get("error") or {}
        emit(ws, "host.error", {"source": "codex", "attempt": ticket,
                                "text": str(error.get("message") or ev.get("message") or "Codex turn failed")})
        return
    item = ev.get("item") or {}
    if not kind.startswith("item.") or not item:
        return
    typ = item.get("type")
    data = {"id": f"{ticket}:{item.get('id', 'unknown')}", "source": "codex",
            "status": item.get("status") or ("completed" if kind == "item.completed" else "in_progress")}
    if typ in ("reasoning", "agent_message"):
        text = item.get("text")
        if isinstance(text, str) and text:
            emit(ws, "thinking" if typ == "reasoning" else "message", {**data, "text": text})
    elif typ == "command_execution":
        emit(ws, "tool", {**data, "name": "bash", "arguments": {"command": item.get("command", "")},
                           "output": str(item.get("aggregated_output") or "")[:MAX_TOOL_CHARS],
                           "exit_code": item.get("exit_code")})
    elif typ == "mcp_tool_call":
        error = item.get("error")
        emit(ws, "tool", {**data, "name": item.get("tool", "tool"), "arguments": item.get("arguments") or {},
                           "output": _tool_text(item.get("result")), "error": str(error)[:2000] if error else None})
    elif typ in ("file_change", "web_search", "todo_list"):
        # Explicit fields only: do not accidentally expose opaque provider data.
        detail = item.get("changes") or item.get("items") or item.get("query") or ""
        emit(ws, "tool", {**data, "name": typ.replace("_", " "), "arguments": {},
                           "output": json.dumps(detail, ensure_ascii=False)[:MAX_TOOL_CHARS]})


def set_active_host(ws: Workspace, key: str, thread_id: str | None = None) -> None:
    write_json(ws.memory / "active_host.json", {"key": key, "thread_id": thread_id, "ts": _now()})


def _latest_context(ws: Workspace, thread_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
    if not thread_id or not all(c.isalnum() or c in "-_" for c in thread_id):
        return None, None
    roots = ws.root / "agent" / "codex" / "sessions"
    paths = sorted(roots.glob(f"**/*{thread_id}*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        # Incremental reads keep a one-second UI poll from rereading long, image-heavy runs.
        with _context_lock:
            stat = path.stat()
            cache = _context_cache.get(str(path))
            if not cache or cache["inode"] != stat.st_ino or stat.st_size < cache["offset"]:
                cache = {"inode": stat.st_ino, "offset": 0, "info": None, "ts": None}
            with path.open("rb") as fh:
                fh.seek(cache["offset"])
                while line := fh.readline():
                    if not line.endswith(b"\n"):
                        break  # retry a concurrent writer's incomplete row on the next poll
                    cache["offset"] = fh.tell()
                    if b'"token_count"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    payload = row.get("payload") or {}
                    if row.get("type") == "event_msg" and payload.get("type") == "token_count" and payload.get("info"):
                        cache.update(info=payload["info"], ts=row.get("timestamp"))
            if len(_context_cache) >= 32 and str(path) not in _context_cache:
                _context_cache.pop(next(iter(_context_cache)))
            _context_cache[str(path)] = cache
            found, measured_at = cache["info"], cache["ts"]
        if found:
            return found, measured_at
    return None, None


def context_usage(ws: Workspace) -> dict[str, Any]:
    session = read_json(ws.memory / "lead_session.json", {}) or {}
    active = read_json(ws.memory / "active_host.json", {}) or {}
    key = active.get("key") or session.get("key")
    thread_id = active.get("thread_id") or session.get("threads", {}).get(key or "", {}).get("thread_id")
    is_codex = bool(key and key.startswith("codex:"))
    info, measured_at = _latest_context(ws, thread_id) if is_codex else (None, None)
    last = (info or {}).get("last_token_usage")
    used = last.get("total_tokens") if last else None
    limit = (info or {}).get("model_context_window")
    source = "codex_token_count" if last else "unavailable"
    estimated = None
    if not is_codex:
        direct = read_json(ws.memory / "provider_context.json", {}) or {}
        # A measurement belongs to one provider/model, never the last provider
        # used elsewhere in this conversation.
        if key and direct.get("key") == key:
            used, limit = direct.get("used_tokens"), direct.get("limit_tokens")
            measured_at, estimated = direct.get("measured_at"), direct.get("estimated_tokens")
            usage = direct.get("breakdown") or {}
            last = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                    "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
                    "reasoning_output_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens")}
            source = "provider_usage" if used is not None else "unavailable"
    billing = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
               "reasoning_output_tokens": 0, "total_tokens": 0, "calls": 0, "source": "gateway_call_log"}
    for row in _rows(ws.ledger / "tools.jsonl"):
        u = row.get("usage") or {}
        if not any(k in u for k in ("input_tokens", "output_tokens", "prompt_token_count", "candidates_token_count")):
            continue
        inp = u.get("input_tokens", u.get("prompt_token_count", 0)) or 0
        out = u.get("output_tokens", u.get("candidates_token_count", 0)) or 0
        billing["calls"] += 1
        billing["input_tokens"] += inp
        billing["output_tokens"] += out
        billing["cached_input_tokens"] += (u.get("input_tokens_details") or {}).get("cached_tokens", u.get("cached_content_token_count", 0)) or 0
        billing["reasoning_output_tokens"] += (u.get("output_tokens_details") or {}).get("reasoning_tokens", u.get("thoughts_token_count", 0)) or 0
        billing["total_tokens"] += u.get("total_tokens", u.get("total_token_count", inp + out)) or 0
    return {"context": {"used_tokens": used, "limit_tokens": limit,
                         "remaining_tokens": max(0, limit - used) if used is not None and limit else None,
                         "percent": round(100 * used / limit, 2) if used is not None and limit else None,
                         "source": source, "measured_at": measured_at, "estimated_tokens": estimated,
                         "label": "Last reported context", "breakdown": last},
            "billing": billing, "model": key.split(":", 1)[1] if key and ":" in key else None,
            "thread_id": thread_id, "compact_available": bool(is_codex and thread_id)}
