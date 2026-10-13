"""Usage per vendor and model for a request, from the gateway's call log (every model call is logged,
including the calls containerised agents make through the model proxy). Usage only: prices for these
models are not in the build, so no money figures are invented."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from vwb.util.jsonio import read_jsonl
from vwb.workspace import Workspace


def _tokens(u: dict[str, Any]) -> dict[str, int]:
    """Normalise the usage shapes of the Responses API, Gemini, the Interactions API and the image API."""
    g = lambda *keys: next((int(u[k]) for k in keys if isinstance(u.get(k), (int, float))), 0)  # noqa: E731
    details = u.get("output_tokens_details") or {}
    cached_in = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    return {"input": g("input_tokens", "prompt_token_count", "total_input_tokens"),
            "output": g("output_tokens", "candidates_token_count", "total_output_tokens"),
            "reasoning": int(details.get("reasoning_tokens") or 0) or g("thoughts_token_count", "total_thought_tokens"),
            "cached": int(cached_in) or g("cached_content_token_count", "total_cached_tokens")}


def summary(ws: Workspace) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"calls": 0, "failed": 0, "seconds": 0.0,
                                                                     "input": 0, "output": 0, "reasoning": 0, "cached": 0})
    for r in read_jsonl(ws.ledger / "tools.jsonl"):
        if not r.get("provider"):
            continue
        row = rows[(r["provider"], r.get("model") or "?")]
        row["calls"] += 1
        row["failed"] += 0 if r.get("ok", True) else 1
        row["seconds"] += float(r.get("seconds") or 0)
        for k, v in _tokens(r.get("usage") or {}).items():
            row[k] += v
    return [{"provider": p, "model": m, **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in d.items()}}
            for (p, m), d in sorted(rows.items(), key=lambda kv: -kv[1]["calls"])]
