"""Stop the owned ngrok child when Workbench exits, including abrupt parent death.

Workbench holds this process's stdin pipe. It is never a user terminal or a
credential channel. Closing the pipe is the normal shutdown protocol.
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys


def supervise(command: list[str]) -> int:
    child = None

    def stop(signum, frame):
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
        while child.poll() is None:
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], .2)
            if readable and not os.read(sys.stdin.fileno(), 1):
                return 0
        return child.returncode
    finally:
        if child and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


if __name__ == '__main__':
    sys.exit(supervise(sys.argv[1:]))
