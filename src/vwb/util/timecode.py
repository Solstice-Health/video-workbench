"""Frame and time conversions. Frames are the unit of truth; seconds are for people.

Intervals are half-open frame ranges [f0, f1), as in the spec's observation envelope.
"""

from __future__ import annotations

import math
import re
from fractions import Fraction


def fps_fraction(num: int, den: int = 1) -> Fraction:
    return Fraction(num, den)


def frame_to_sec(frame: float, fps: Fraction | float) -> float:
    return float(frame) / float(fps)


def sec_to_frame(sec: float, fps: Fraction | float, rounding: str = "nearest") -> int:
    exact = sec * float(fps)
    if rounding == "floor":
        return int(math.floor(exact + 1e-6))
    if rounding == "ceil":
        return int(math.ceil(exact - 1e-6))
    return int(round(exact))


def fmt(sec: float, ms: bool = True) -> str:
    """Seconds as mm:ss.mmm (or hh:mm:ss.mmm past an hour)."""
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    hours, rem = divmod(sec, 3600)
    minutes, seconds = divmod(rem, 60)
    body = f"{int(minutes):02d}:{seconds:06.3f}" if ms else f"{int(minutes):02d}:{int(seconds):02d}"
    if hours >= 1:
        body = f"{int(hours)}:{body}"
    return sign + body


_TC = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")


def parse(value: str | float | int) -> float:
    """Parse seconds from 'mm:ss.mmm', 'hh:mm:ss', '12.5', '12.5s' or a number."""
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip().lower().rstrip("s")
    m = _TC.match(text)
    if m:
        hours = int(m.group(1) or 0)
        return hours * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return float(text)
