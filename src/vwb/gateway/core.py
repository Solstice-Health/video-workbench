"""Gateway core: slot resolution, retries, fallbacks and the call log."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vwb.config import RUNS_DIR, secrets, slot
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import canonical_json
from vwb.util.log import log
from vwb.workspace.ledger import CallLog, Ledger

_TRANSIENT = re.compile(r"(429|500|502|503|504|overloaded|rate.?limit|timeout|timed out|temporarily|unavailable|"
                        r"connection|reset by peer|RESOURCE_EXHAUSTED|INTERNAL|DEADLINE)", re.I)


@dataclass
class SlotCall:
    """Result of one gateway call."""
    slot: str
    provider: str
    model: str
    text: str
    data: Any = None                      # parsed JSON when a schema was requested
    usage: dict[str, Any] = field(default_factory=dict)
    fallback: bool = False
    raw: Any = None


def parse_json_loose(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and leading prose."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Model reply is not JSON: {text[:300]!r}")


class Gateway:
    """One gateway per request workspace (or a global one with no workspace)."""

    _clients: dict[str, Any] = {}
    _client_lock = threading.Lock()

    def __init__(self, ws=None, actor: str = "control_plane"):
        self.ws = ws
        self.actor = actor
        log_path = (ws.ledger / "tools.jsonl") if ws else (RUNS_DIR / "_gateway_calls.jsonl")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        self.calls = CallLog(log_path)
        self.ledger = Ledger(ws.ledger / "ledger.jsonl") if ws else None

    # ---- clients ----
    def google(self):
        with self._client_lock:
            if "google" not in self._clients:
                from google import genai
                self._clients["google"] = genai.Client(api_key=secrets().require("google"))
            return self._clients["google"]

    def openai(self):
        with self._client_lock:
            if "openai" not in self._clients:
                from openai import OpenAI
                self._clients["openai"] = OpenAI(api_key=secrets().require("openai"), timeout=1800, max_retries=2)
            return self._clients["openai"]

    # ---- logging ----
    def record(self, *, slot_name: str, provider: str, model: str, purpose: str, request: Any,
               response_text: str, usage: dict | None, seconds: float, ok: bool, error: str = "") -> None:
        self.calls.record(actor=self.actor, slot=slot_name, provider=provider, model=model, purpose=purpose,
                          input_sha256=sha256_text(canonical_json(request)) if request is not None else None,
                          output_sha256=sha256_text(response_text or ""), usage=usage or {},
                          seconds=round(seconds, 2), ok=ok, error=error[:500])

    def flag_fallback(self, slot_name: str, wanted: str, served: str, reason: str) -> None:
        if self.ledger is None:
            return
        from vwb.workspace import flags
        flag = flags.make("SLOT-FALLBACK", f"Slot {slot_name} served by {served} instead of {wanted}",
                          subject=f"{slot_name}:{served}", evidence={"reason": reason[:300]},
                          raised_by="gateway")
        self.ledger.append("flag.raised", "gateway", flag.to_dict())

    # ---- retries and fallbacks ----
    def resolve_slot(self, slot_name: str) -> dict[str, Any]:
        """Host-owned slot selection; specialised callers may pin a configured model."""
        return slot(slot_name)

    def with_fallbacks(self, slot_name: str, fn: Callable[[dict[str, Any]], SlotCall],
                       attempts: int = 4) -> SlotCall:
        """Call fn(slot_config) with retries; on persistent failure try each fallback config."""
        primary = self.resolve_slot(slot_name)
        configs = [primary] + [dict(primary, **fb) for fb in primary.get("fallbacks", [])]
        last_error: Exception | None = None
        for i, cfg in enumerate(configs):
            for attempt in range(attempts):
                try:
                    result = fn(cfg)
                    if i > 0:
                        result.fallback = True
                        self.flag_fallback(slot_name, primary.get("model", "?"), cfg.get("model", "?"), str(last_error))
                    return result
                except Exception as exc:  # noqa: BLE001 - classify below
                    last_error = exc
                    transient = bool(_TRANSIENT.search(str(exc)))
                    log.warning("gateway %s via %s failed (%s, attempt %d): %s", slot_name, cfg.get("model"),
                                "transient" if transient else "permanent", attempt + 1, str(exc)[:240])
                    if not transient:
                        break
                    time.sleep(min(60, 4 * (2 ** attempt)))
        raise RuntimeError(f"Slot {slot_name} failed on every model: {last_error}") from last_error
