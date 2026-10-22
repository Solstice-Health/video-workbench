"""Detached trusted supervisor for one Docker sandbox job; never executes agent code on host."""
from __future__ import annotations

import json
import os
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
    for name, value in row.get('host_config', {}).items():
        if name in ('VWB_ENV_FILE', 'VWB_RUNS_DIR') and isinstance(value, str):
            os.environ[name] = value
    # Configuration is imported before this private receipt is read. Its runs
    # root must match the launching host for managed provider keys to resolve.
    if os.environ.get('VWB_RUNS_DIR'):
        import vwb.config
        vwb.config.RUNS_DIR = Path(os.environ['VWB_RUNS_DIR'])
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(2):
            if json.loads(path.read_text())['status'] in TERMINAL:
                return
            update(directory, heartbeat=time.time())

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        update(directory, status='running', heartbeat=time.time(), started=time.time())
        state = {}
        gateway = None
        if row['arguments'].get('models'):
            from vwb.gateway import Gateway
            from vwb.tools.sandbox_tools import MODEL_TOOLS
            permissions = row.get('model_permissions')
            if (not isinstance(permissions, list) or not permissions
                    or any(name not in MODEL_TOOLS for name in permissions) or not row.get('role')):
                raise ValueError('Missing original model-tool permissions')
            state['allowed_tools'] = permissions
            gateway = Gateway(ws, actor=row['owner'])
        ctx = ToolContext(ws=ws, agent=row['owner'], role=row.get('role', 'job_lead'), gateway=gateway, state=state)
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
