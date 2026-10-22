"""Trusted native-render watchdog: parent EOF or deadline stops only the owned group.

The GUI After Effects process is never a target. A separate liveness pipe handles
an abruptly killed supervisor; PID start time and argv fence PID reuse.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import sys
import time
from pathlib import Path

from vwb.desktop.after_effects import _read_json, process_identity


def watch(path):
    row = _read_json(path)
    if row.get('kind') != 'render' or type(row.get('command_pid')) is not int or not row.get('command_identity'):
        raise ValueError('Watchdog requires a pinned owned renderer')
    pid, identity = row['command_pid'], row['command_identity']
    deadline = time.monotonic() + row['timeout_s'] + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        ready, _, _ = select.select([sys.stdin.buffer], [], [], .25)
        if ready and not os.read(sys.stdin.fileno(), 1):
            break
        if (path.parent / 'cancel').exists():
            break
    if process_identity(pid) != identity:
        return
    try:
        if os.getpgid(pid) != pid:
            return
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with contextlib.suppress(ProcessLookupError):
            if process_identity(pid) != identity:
                return
        time.sleep(.1)
    if process_identity(pid) == identity:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


if __name__ == '__main__':
    watch(Path(sys.argv[1]))
