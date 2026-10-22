"""Durable bounded automation jobs, supervised outside a single chat turn."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = {'completed', 'failed', 'cancelled', 'timed_out', 'terminated', 'interrupted'}
JOB_ID = re.compile(r'[a-f0-9]{32}\Z')


def _root(ws):
    path = ws.root / '.sandbox' / 'jobs'
    for parent in (ws.root / '.sandbox', path):
        if parent.is_symlink():
            raise ValueError('Sandbox state must not be a symlink')
        parent.mkdir(exist_ok=True)
    return path


@contextmanager
def _locked(ws):
    path = _root(ws) / '.lock'
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


@contextmanager
def _job_lock(directory):
    with (directory / '.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _write(directory, row):
    tmp = directory / ('job-' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(row))
    tmp.replace(directory / 'job.json')


def update(directory, **changes):
    with _job_lock(directory):
        row = json.loads((directory / 'job.json').read_text())
        if row['status'] in TERMINAL and changes.get('status', row['status']) != row['status']:
            # A late heartbeat/result cannot resurrect a job or overwrite the
            # cancellation/interruption chosen by a concurrent control process.
            changes = {key: value for key, value in changes.items()
                       if key not in {'status', 'ended', 'note', 'error'}}
        row.update(changes)
        _write(directory, row)
    return row


def _read(directory):
    interrupted = False
    with _job_lock(directory):
        row = json.loads((directory / 'job.json').read_text())
        if row['status'] not in TERMINAL and time.time() - row['heartbeat'] > 45:
            (directory / 'cancel').touch()
            row.update(status='interrupted', ended=time.time(),
                       note='The supervisor stopped responding. No command was replayed; inspect outputs before retrying.')
            _write(directory, row)
            interrupted = True
    if interrupted:
        from vwb.tools.sandbox_tools import _remove
        _remove('vwb-sandbox-' + row['id'])
    return row


def _public(row):
    return {key: value for key, value in row.items()
            if key not in {'request_root', 'arguments', 'supervisor_pid', 'host_config', 'model_permissions'}}


def launch(ctx, args):
    from vwb.tools.sandbox_tools import _arguments, _cancelled, _model_permissions, _options
    _arguments(ctx, args)
    _options(args)
    if _cancelled(ctx):
        raise ValueError('Sandbox command cancelled before launch')
    with _locked(ctx.ws):
        rows = [_read(path) for path in _root(ctx.ws).iterdir() if JOB_ID.fullmatch(path.name)]
        if sum(row['status'] not in TERMINAL for row in rows) >= 4:
            raise ValueError('Four sandbox jobs are already active for this request. Wait or cancel one first.')
        job_id = uuid.uuid4().hex
        directory = _root(ctx.ws) / job_id
        directory.mkdir()
        permissions = _model_permissions(ctx) if args.get('models') else []
        if args.get('models') and not permissions:
            raise ValueError('This agent has no inherited model-tool permissions.')
        row = {'id': job_id, 'owner': ctx.agent, 'role': ctx.role, 'status': 'starting', 'created': time.time(),
               'heartbeat': time.time(), 'request_root': str(ctx.ws.root.resolve()),
               'arguments': {**args, 'background': False}, 'timeout_s': args.get('timeout_s', 60),
               'network': args.get('network', 'none'),
               'models': bool(args.get('models')), 'model_permissions': permissions,
               # Host configuration paths are private metadata, outside the
               # mounted work folder. They are never injected into Docker.
               'host_config': {key: os.environ[key] for key in ('VWB_ENV_FILE', 'VWB_RUNS_DIR') if key in os.environ},
               'note': 'Background automation survives chat turns. Use sandbox_status(cancel=true) to stop it. '
                       'Outputs written under work/ remain; cancellation does not roll them back.'}
        (directory / 'job.json').write_text(json.dumps(row))
        # The supervisor runs trusted harness code, never agent code on the host.
        # Keys, .env paths and provider settings are deliberately not inherited.
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'DOCKER_HOST', 'DOCKER_CONTEXT'}}
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
        try:
            with (directory / 'supervisor.log').open('wb') as output:
                process = subprocess.Popen([sys.executable, '-m', 'vwb.sandbox.worker', str(directory / 'job.json')],
                                           stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                           env=environment, start_new_session=True, close_fds=True)
            row = update(directory, supervisor_pid=process.pid)
        except OSError:
            update(directory, status='failed', ended=time.time(), note='Could not launch the sandbox supervisor.')
            raise
    return _public(row)


def status(ctx, args):
    wait = args.get('wait_s', 0)
    if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 30:
        raise ValueError('wait_s must be between 0 and 30 seconds')
    if type(args.get('cancel', False)) is not bool:
        raise ValueError('cancel must be a boolean')
    for name in ('stdout_offset', 'stderr_offset'):
        if type(args.get(name, 0)) is not int or args.get(name, 0) < 0:
            raise ValueError(name + ' must be a nonnegative byte offset')
    job_id = args.get('job_id')
    if not job_id:
        if args.get('cancel'):
            raise ValueError('Choose a job_id to cancel')
        rows = [_read(path) for path in _root(ctx.ws).iterdir() if JOB_ID.fullmatch(path.name)]
        rows.sort(key=lambda row: row['created'], reverse=True)
        return {'jobs': [_public(row) for row in rows[:100]], 'truncated': len(rows) > 100}
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise ValueError('Invalid sandbox job id')
    directory = _root(ctx.ws) / job_id
    if not (directory / 'job.json').is_file():
        raise ValueError('Sandbox job not found in this request')
    row = _read(directory)
    if args.get('cancel') and row['status'] not in TERMINAL:
        (directory / 'cancel').touch()
    deadline = time.monotonic() + wait
    while row['status'] not in TERMINAL and time.monotonic() < deadline:
        time.sleep(.1)
        row = _read(directory)
    result = _public(row)
    result['cancel_requested'] = (directory / 'cancel').exists()
    for stream in ('stdout', 'stderr'):
        offset = args.get(stream + '_offset', 0)
        path = directory / (stream + '.log')
        data = b''
        size = path.stat().st_size if path.is_file() else 0
        if offset > size:
            raise ValueError(stream + '_offset is beyond the saved output')
        if size:
            with path.open('rb') as handle:
                handle.seek(offset)
                data = handle.read(16_000)
        result[stream] = data.decode('utf-8', errors='replace')
        result[stream + '_next_offset'] = offset + len(data)
        result[stream + '_more'] = size > offset + len(data)
    return result
