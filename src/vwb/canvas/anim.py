"""Keyframe evaluation, shared by the compositor, the checks and (mirrored) the Remotion renderer."""

from __future__ import annotations

from typing import Any


def ease(t: float, kind: str) -> float:
    t = min(max(t, 0.0), 1.0)
    if kind == "hold":
        return 0.0
    if kind == "linear":
        return t
    if kind == "in":
        return t * t * t
    if kind == "out":
        return 1 - (1 - t) ** 3
    return 4 * t * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 3) / 2   # inout


def is_keyed(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and (isinstance(value[0], dict) or hasattr(value[0], "f"))


def _k(key: Any) -> tuple[int, Any, str]:
    if isinstance(key, dict):
        return int(key["f"]), key["v"], key.get("ease", "inout")
    return int(key.f), key.v, key.ease


def _rgba(h: str) -> list[float]:
    v = h.lstrip("#")
    v = v + "FF" if len(v) == 6 else v
    return [int(v[i:i + 2], 16) for i in (0, 2, 4, 6)]


def _hex(c: list[float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(x))):02X}" for x in c)


def value_at(value: Any, f: int) -> Any:
    """Static values pass through. Keyframed numbers, number lists and "#RRGGBB[AA]" colours interpolate."""
    if not is_keyed(value):
        return value
    keys = [_k(k) for k in value]
    if f <= keys[0][0]:
        return keys[0][1]
    for (fa, va, _), (fb, vb, e) in zip(keys, keys[1:], strict=False):
        if fa <= f < fb:
            if e == "hold":
                return va
            u = ease((f - fa) / max(1, fb - fa), e)
            if isinstance(va, (int, float)):
                return va + (vb - va) * u
            if isinstance(va, str):
                return _hex([a + (b - a) * u for a, b in zip(_rgba(va), _rgba(vb), strict=True)])
            return [a + (b - a) * u for a, b in zip(va, vb, strict=True)]
    return keys[-1][1]


def key_frames(value: Any) -> list[int]:
    return [_k(k)[0] for k in value] if is_keyed(value) else []


def all_values(value: Any) -> list[Any]:
    """Every key value (or the static value), for range checks."""
    return [_k(k)[1] for k in value] if is_keyed(value) else [value]


def active(span: list[int] | None, f: int) -> bool:
    return span is None or (span[0] <= f < span[1])
