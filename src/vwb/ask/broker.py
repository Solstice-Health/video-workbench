"""The question broker. Agents ask; the broker answers from memory, merges duplicates,
times out to the recommended option, and never blocks a request (spec D43, D04).

Tickets live in asks/tickets/<id>.json. Answers are signed human events in the ledger and
are remembered by key and lane in asks/memory.jsonl (brand preferences, brief amendments).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from filelock import FileLock

from vwb.util.hashing import new_id, sha256_text
from vwb.util.jsonio import append_jsonl, read_json, read_jsonl, write_json
from vwb.workspace import Workspace, flags
from vwb.workspace.ledger import Ledger

KINDS = {"choose", "supply", "allow", "confirm", "clarify"}
LANES = {"editor", "brief", "sealed"}
NEVER_DEFAULT = {"supply", "allow"}   # grants and source files stay parked; a permission is never assumed


def _now() -> datetime:
    return datetime.now(UTC)


class Broker:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.dir = ws.asks / "tickets"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.memory_path = ws.asks / "memory.jsonl"
        self.lock = FileLock(str(ws.asks / ".lock"))
        self.ledger = Ledger(ws.ledger / "ledger.jsonl")

    def _path(self, tid: str):
        return self.dir / f"{tid}.json"

    def tickets(self, status: str | None = None) -> list[dict[str, Any]]:
        # A timeout must not overwrite an answer signed between this read and write.
        # FileLock is reentrant, including when ask() calls tickets() while holding it.
        with self.lock:
            out = [read_json(p) for p in sorted(self.dir.glob("*.json"))]
            self._apply_timeouts(out)
            return [t for t in out if status is None or t["status"] == status]

    def _memory_answer(self, key: str, lane: str) -> dict[str, Any] | None:
        for row in reversed(read_jsonl(self.memory_path)):
            if row["key"] == key and row["lane"] == lane:
                return row
        return None

    def ask(self, *, asker: str, question: str, why: str, options: list[dict[str, Any]], recommended: str,
            kind: str = "choose", lane: str = "editor", scope: str = "request", key: str | None = None,
            parks: str | None = None, deadline_s: float = 1800, on_timeout: str | None = None) -> dict[str, Any]:
        if kind not in KINDS or lane not in LANES:
            raise ValueError(f"kind must be one of {sorted(KINDS)} and lane one of {sorted(LANES)}")
        if not 2 <= len(options) <= 4 and kind != "supply":
            raise ValueError("offer 2 to 4 options")
        if recommended not in {o["id"] for o in options} and kind != "supply":
            raise ValueError("the recommended option must be one of the options")
        key = key or f"{kind}:{sha256_text(question.strip().lower())[:16]}"
        on_timeout = on_timeout or ("keep_parked" if kind in NEVER_DEFAULT else "use_recommended")
        with self.lock:
            remembered = self._memory_answer(key, lane)
            if remembered:
                return {"id": None, "status": "answered_from_memory", "answer": remembered["answer"],
                        "by": remembered["by"], "cited": remembered["ticket"], "key": key}
            for t in self.tickets(status="open"):
                if t["key"] == key and t["lane"] == lane:
                    if asker not in t["askers"]:
                        t["askers"].append(asker)
                        write_json(self._path(t["id"]), t)
                    return {**t, "merged": True}
            tid = new_id("ask")
            ticket = {"id": tid, "status": "open", "question": question, "why": why, "options": options,
                      "recommended": recommended, "kind": kind, "lane": lane, "scope": scope, "key": key,
                      "parks": parks, "askers": [asker], "created": _now().isoformat(timespec="seconds"),
                      "deadline": (_now() + timedelta(seconds=deadline_s)).isoformat(timespec="seconds"),
                      "on_timeout": on_timeout, "answer": None}
            write_json(self._path(tid), ticket)
        self.ledger.append("ask.created", asker, {"ticket": tid, "question": question, "kind": kind, "lane": lane,
                                                   "recommended": recommended, "key": key})
        flag = flags.make("ASK-PENDING", f"Question open: {question[:120]} (default: {recommended})",
                          subject=f"ask:{tid}", evidence={"ticket": tid}, raised_by="broker")
        self.ledger.append("flag.raised", "broker", flag.to_dict())
        return ticket

    def answer(self, tid: str, option: str | None, text: str = "", by: str = "human") -> dict[str, Any]:
        """A signed human event. Only the CLI (the chat gateway) calls this, never an agent tool."""
        with self.lock:
            t = read_json(self._path(tid))
            if t is None:
                raise KeyError(f"No ticket {tid}")
            if option and t["kind"] != "supply" and option not in {o["id"] for o in t["options"]}:
                raise ValueError(f"{option!r} is not an option of {tid}")
            t["status"] = "answered"
            t["answer"] = {"option": option, "text": text, "by": by, "at": _now().isoformat(timespec="seconds")}
            write_json(self._path(tid), t)
        ev = self.ledger.append("human.answer", f"human:{by}", {"ticket": tid, "option": option, "text": text,
                                                                "lane": t["lane"], "key": t["key"]}, signed=True)
        append_jsonl(self.memory_path, {"key": t["key"], "lane": t["lane"], "answer": t["answer"], "by": by,
                                        "ticket": tid, "event": ev["hash"]})
        self.ledger.append("flag.cleared", "broker", {"id": flags.make("ASK-PENDING", "", subject=f"ask:{tid}").id})
        return t

    def _apply_timeouts(self, tickets: list[dict[str, Any]]) -> None:
        now = _now()
        for t in tickets:
            if t["status"] != "open" or datetime.fromisoformat(t["deadline"]) > now:
                continue
            if t["on_timeout"] == "use_recommended":
                t["status"] = "defaulted"
                t["answer"] = {"option": t["recommended"], "text": "", "by": "default", "at": now.isoformat(timespec="seconds")}
                write_json(self._path(t["id"]), t)
                f = flags.make("ASSUMED-DEFAULT", f"No answer by the deadline; used the recommended option "
                               f"'{t['recommended']}' for: {t['question'][:100]}", subject=f"ask-default:{t['id']}",
                               raised_by="broker")
                self.ledger.append("flag.raised", "broker", f.to_dict())
