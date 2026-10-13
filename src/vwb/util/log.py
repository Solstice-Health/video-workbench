"""One logger and one console for the whole harness."""

from __future__ import annotations

import logging
import os

from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=True)

log = logging.getLogger("vwb")
if not log.handlers:
    handler = RichHandler(console=console, show_path=False, rich_tracebacks=True, markup=False)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(os.environ.get("VWB_LOG_LEVEL", "INFO").upper())
    log.propagate = False
