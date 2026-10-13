"""A finding: one check's result with evidence. Flags are derived from findings by code."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Finding:
    check: str                      # e.g. "t0.delivery", "t1.text_readback"
    status: str                     # pass | fail | cannot_tell | info
    kind: str | None = None         # flag kind a failure maps to (from the catalog)
    summary: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    bumps: list[str] = field(default_factory=list)
    level: str | None = None        # code-computed severity (tables), else the catalog decides
    tier: str = "T0"
    qualified: bool = False         # no seeded qualification yet (spec D12)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
