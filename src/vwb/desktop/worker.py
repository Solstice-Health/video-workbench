"""One durable native creative-app job; no credentials or automatic command replay."""

from __future__ import annotations

import sys
from pathlib import Path

from vwb.desktop.after_effects import execute_job

if __name__ == '__main__':
    execute_job(Path(sys.argv[1]).parent)
