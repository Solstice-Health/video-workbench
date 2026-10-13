"""The model gateway (control plane). It holds the vendor keys, resolves model slots,
retries and falls back, and logs every outbound call with hashes (invariant I4)."""

from vwb.gateway.core import Gateway, SlotCall

__all__ = ["Gateway", "SlotCall"]
