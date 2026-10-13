"""The append-only, hash-chained ledger (spec invariant I1) and the gateway call log (I4).

Every event carries the hash of the previous one, so a deleted or edited line breaks the
chain and raises AUDIT-GAP. Human events carry an HMAC signature from a key that only the
control plane holds; agents cannot mint them (spec: "no channel carries authority").
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock

from vwb.config import RUNS_DIR
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import append_jsonl, canonical_json, read_jsonl

GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def control_key() -> bytes:
    """The control plane's signing key. Created once, readable only by the owner."""
    key_path = RUNS_DIR / ".keys" / "control.key"
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(os.urandom(32))
        os.chmod(key_path, 0o600)
    return key_path.read_bytes()


def sign(payload: Any) -> str:
    return hmac.new(control_key(), canonical_json(payload).encode(), hashlib.sha256).hexdigest()


def verify_signature(payload: Any, signature: str) -> bool:
    return hmac.compare_digest(sign(payload), signature or "")


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = FileLock(str(self.path) + ".lock")

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 65536)
            fh.seek(size - block)
            lines = fh.read().splitlines()
            for raw in reversed(lines):
                if raw.strip():
                    last = raw
                    break
        if last is None:
            return 0, GENESIS
        import json
        row = json.loads(last)
        return int(row["seq"]), row["hash"]

    def append(self, kind: str, actor: str, data: dict[str, Any] | None = None, *,
               output: str | None = None, version: int | None = None, signed: bool = False) -> dict[str, Any]:
        with self._lock:
            seq, prev = self._tail()
            event = {"seq": seq + 1, "ts": _now(), "kind": kind, "actor": actor,
                     "output": output, "version": version, "data": data or {}, "prev": prev}
            if signed:
                event["sig"] = sign({k: event[k] for k in ("seq", "kind", "actor", "output", "version", "data")})
            event["hash"] = sha256_text(canonical_json(event))
            append_jsonl(self.path, event)
            return event

    def events(self, kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
        rows = read_jsonl(self.path)
        if kinds is None:
            return rows
        wanted = tuple(kinds)
        return [r for r in rows if r["kind"].startswith(wanted)]

    def verify_chain(self) -> tuple[bool, int | None]:
        """Return (ok, first_bad_seq)."""
        prev = GENESIS
        for row in read_jsonl(self.path):
            body = {k: v for k, v in row.items() if k != "hash"}
            if row.get("prev") != prev or sha256_text(canonical_json(body)) != row.get("hash"):
                return False, row.get("seq")
            prev = row["hash"]
        return True, None


class CallLog:
    """tools.jsonl: every gateway call with input and output hashes (invariant I4)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = FileLock(str(self.path) + ".lock")

    def record(self, **row: Any) -> None:
        row.setdefault("ts", _now())
        with self._lock:
            append_jsonl(self.path, row)
