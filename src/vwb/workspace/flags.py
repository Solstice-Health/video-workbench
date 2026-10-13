"""Flags: recorded notices with evidence. Code picks the severity; agents cannot lower one.

Flags live in the ledger as events (flag.raised, flag.cleared, human.ack, human.waive).
The current state of every flag is derived by replaying the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from vwb.config import load_config
from vwb.util.hashing import sha256_json, short
from vwb.workspace.ledger import Ledger

LEVELS = ["INFO", "WARN", "HOLD", "BLOCK"]
BUMPS = {"mark", "regulated", "time_share", "peak_area", "generative_t2"}


def catalog() -> dict[str, dict[str, Any]]:
    return load_config("flag_kinds")["kinds"]


def base_kind(kind: str) -> str:
    return kind.split(":", 1)[0]


def severity(kind: str, bumps: Iterable[str] = (), override_base: str | None = None) -> str:
    """severity = min(max[kind], base[kind] + bump). One +1 bump at most (spec)."""
    entry = catalog().get(base_kind(kind))
    if entry is None:
        raise KeyError(f"Unknown flag kind {kind!r}; add it to config/flag_kinds.yaml")
    base = LEVELS.index(override_base or entry["base"])
    cap = LEVELS.index(entry["max"])
    bump = 1 if set(bumps) & BUMPS else 0
    return LEVELS[min(cap, base + bump)]


def text_unverified_severity(readers_agree: bool, regulated: bool) -> str:
    """Spec TEXT-UNVERIFIED table: non-regulated WARN; regulated HOLD if readers agree, else BLOCK."""
    if not regulated:
        return "WARN"
    return "HOLD" if readers_agree else "BLOCK"


@dataclass
class Flag:
    kind: str
    summary: str
    severity: str
    output: str | None = None
    version: int | None = None
    subject: str = ""                       # what the flag is about, used for the stable id
    evidence: dict[str, Any] = field(default_factory=dict)
    raised_by: str = "verifier"
    status: str = "open"                    # open | acknowledged | waived | cleared
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "f-" + short(sha256_json([self.kind, self.output, self.subject]), 10)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "severity": self.severity, "summary": self.summary,
                "output": self.output, "version": self.version, "subject": self.subject,
                "evidence": self.evidence, "raised_by": self.raised_by, "status": self.status}


def make(kind: str, summary: str, *, output: str | None = None, version: int | None = None,
         subject: str = "", evidence: dict | None = None, bumps: Iterable[str] = (),
         level: str | None = None, raised_by: str = "verifier") -> Flag:
    """Build a flag. `level` is for code-computed tables (e.g. TEXT-UNVERIFIED); it is capped at the kind's max."""
    entry = catalog().get(base_kind(kind))
    if entry is None:
        raise KeyError(f"Unknown flag kind {kind!r}; add it to config/flag_kinds.yaml")
    if level is None:
        sev = severity(kind, bumps)
    else:
        sev = LEVELS[min(LEVELS.index(entry["max"]), LEVELS.index(level))]
    return Flag(kind=kind, summary=summary, severity=sev, output=output, version=version,
                subject=subject or summary, evidence=evidence or {}, raised_by=raised_by)


def current_flags(ledger: Ledger, output: str | None = None, version: int | None = None) -> list[dict[str, Any]]:
    """Replay the ledger into the current state of each flag."""
    state: dict[str, dict[str, Any]] = {}
    for ev in ledger.events():
        kind = ev["kind"]
        data = ev.get("data", {})
        if kind == "flag.raised":
            flag = dict(data)
            prev = state.get(flag["id"])
            if prev and prev["status"] in ("acknowledged", "waived") and prev.get("version") == flag.get("version"):
                flag["status"] = prev["status"]
            state[flag["id"]] = flag
        elif kind == "flag.cleared" and data.get("id") in state:
            state[data["id"]]["status"] = "cleared"
            state[data["id"]]["cleared_by"] = ev["actor"]
        elif kind in ("human.ack", "human.waive") and data.get("id") in state:
            state[data["id"]]["status"] = "acknowledged" if kind == "human.ack" else "waived"
            state[data["id"]]["by"] = ev["actor"]
    flags = list(state.values())
    if output is not None:
        flags = [f for f in flags if f.get("output") in (output, None)]
    if version is not None:
        flags = [f for f in flags if f.get("version") in (version, None)]
    return flags


def triage(flags: list[dict[str, Any]], attested: bool, checks_qualified: bool) -> str:
    """RED: open BLOCK or no attestation. YELLOW: open WARN/HOLD or unqualified checks. Else GREEN."""
    open_flags = [f for f in flags if f.get("status") == "open"]
    if not attested or any(f["severity"] == "BLOCK" for f in open_flags):
        return "RED"
    if not checks_qualified or any(f["severity"] in ("WARN", "HOLD") for f in open_flags):
        return "YELLOW"
    return "GREEN"
