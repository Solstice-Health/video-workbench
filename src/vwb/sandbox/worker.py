"""Detached trusted supervisor for one Docker sandbox job; never executes agent code on host."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from vwb.sandbox.jobs import TERMINAL, update
from vwb.tools.base import ToolContext
from vwb.tools.sandbox_tools import _execute, _remove
from vwb.workspace import Workspace


def main(path):
    directory = path.parent
    row = json.loads(path.read_text())
    ws = Workspace(Path(row['request_root']))
    if path.resolve() != (ws.root / '.sandbox/jobs' / row['id'] / 'job.json').resolve():
        raise ValueError('Invalid sandbox supervisor receipt path')
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(2):
            if json.loads(path.read_text())['status'] in TERMINAL:
                return
            update(directory, heartbeat=time.time())

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        update(directory, status='running', heartbeat=time.time(), started=time.time())
        ctx = ToolContext(ws=ws, agent=row['owner'], role='job_lead', gateway=None)
        result = _execute(ctx, row['arguments'], job={'id': row['id'], 'directory': directory})
        data = result.data if isinstance(result.data, dict) else {'status': 'failed', 'error': result.text}
        # Output is retained in bounded paged logs instead of duplicated in the receipt.
        data.pop('stdout', None)
        data.pop('stderr', None)
        update(directory, **data, ended=time.time(), heartbeat=time.time())
    except Exception:
        _remove('vwb-sandbox-' + row['id'])
        update(directory, status='failed', ended=time.time(), note='Sandbox execution failed. Inspect the job output.')
    finally:
        stop.set()


if __name__ == '__main__':
    main(Path(sys.argv[1]))
