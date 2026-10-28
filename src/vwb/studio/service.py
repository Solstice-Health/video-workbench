"""Durable jobs with bounded host workers, explicit cancellation and no restart replay."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from pydantic import ValidationError

from vwb.agents.events import emit
from vwb.studio.assets import directory, snapshot_input
from vwb.studio.models import JobInput, capabilities
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import canonical_json, read_json, write_json
from vwb.workspace.request import now_iso

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
JOB_ID = re.compile(r'[a-f0-9]{32}\Z')


def private_job(ws, job_id: str) -> Path:
    if not JOB_ID.fullmatch(job_id):
        raise ValueError('Invalid studio job id.')
    return directory(ws, '.studio/jobs') / f'{job_id}.json'


def prepare(ws, spec: JobInput, job_id: str) -> dict:
    family = 'images' if spec.kind == 'image' else 'videos'
    folder = directory(ws, f'work/studio/{family}/{job_id}/inputs')
    video_input = spec.kind == 'video' and spec.operation in ('edit', 'extend')
    inputs = [snapshot_input(ws, value, folder, str(i), kind='video' if video_input else 'image')
              for i, value in enumerate(spec.inputs)]
    mask = snapshot_input(ws, spec.mask, folder, 'mask', kind='mask') if spec.mask else None
    if mask and (mask['width'], mask['height']) != (inputs[0]['width'], inputs[0]['height']):
        raise ValueError('The mask dimensions must match the input image exactly.')
    public = spec.public()
    if spec.kind == 'image' and spec.operation == 'inpaint':
        public['effective_size'] = [inputs[0]['width'], inputs[0]['height']]
    if spec.kind == 'image' and spec.operation == 'remove_background':
        public['source_size'] = [inputs[0]['width'], inputs[0]['height']]
        public['effective_size'] = None  # alpha-cropped size is known only after extraction
        if spec.background_method == 'matte' and not spec.box:
            W, H = public['source_size']
            if min(W, H) < 5:
                raise ValueError('Local background masking needs an image at least 5 pixels wide and high.')
            # The matte tool preserves the whole source region and seeds
            # GrabCut with an inset rectangle when no background border exists.
            public['box'] = [0, 0, W, H]
    if spec.place:
        x, y, w, h = spec.place
        W, H = public['effective_size']
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > W or y + h > H:
            raise ValueError('Place the source fully inside the output canvas with positive dimensions.')
        if x == y == 0 and w == W and h == H:
            raise ValueError('Leave some canvas outside the source to outpaint.')
    if spec.box:
        x, y, w, h = spec.box
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > inputs[0]['width'] or y + h > inputs[0]['height']:
            raise ValueError('Keep the subject region inside the source image.')
    if spec.operation == 'outpaint' and spec.kind == 'image' and spec.place is None:
        W, H = public['effective_size']
        w, h = inputs[0]['width'], inputs[0]['height']
        if w >= W or h >= H:
            raise ValueError('Set a source placement inside the larger output canvas to outpaint.')
        public['place'] = [(W - w) // 2, (H - h) // 2, w, h]
    return {'input': public, 'prepared': {'inputs': inputs, 'mask': mask},
            'output_folder': ws.rel(folder.parent)}


def worker_environment() -> dict[str, str]:
    names = {'PATH', 'HOME', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'VIRTUAL_ENV', 'PYTHONPATH',
             'VWB_ENV_FILE', 'VWB_RUNS_DIR', 'OPENAI_API_KEY', 'GOOGLE_API_KEY', 'SSL_CERT_FILE',
             'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE'}
    return {key: value for key, value in os.environ.items() if key in names}


class StudioJobs:
    def __init__(self, *, command: list[str] | None = None, concurrency: int = 2, timeout: float = 3600):
        self.command = command or [sys.executable, '-m', 'vwb.studio.worker']
        self.lock = threading.RLock()
        self.semaphore = threading.Semaphore(concurrency)
        self.closed = False
        self.timeout = timeout
        self.owned: dict[str, dict] = {}

    def _read(self, ws, job_id: str) -> dict:
        path = private_job(ws, job_id)
        if path.is_symlink():
            raise ValueError('Studio state must be a regular file.')
        row = read_json(path)
        if not row:
            raise FileNotFoundError('That studio job was not found.')
        if row['status'] not in TERMINAL and job_id not in self.owned:
            row.update(status='interrupted', ended=now_iso(), error='The server stopped. Start a new job to retry; no generation was replayed.')
            write_json(path, row)
        return row

    @staticmethod
    def public(row: dict) -> dict:
        result = {key: value for key, value in row.items() if key not in {'prepared', 'fingerprint', 'request_root'}}
        result['reusable_inputs'] = row.get('prepared', {}).get('inputs', [])
        result['reusable_mask'] = row.get('prepared', {}).get('mask')
        if row.get('result'):
            # Older completed jobs kept quality notes only in tool_result. Show
            # those notes without rewriting their saved result or receipt.
            saved = row['result']
            notes = (saved.get('tool_result') or {}).get('notes', [])
            warnings = saved.get('warnings', [])
            values = (warnings if isinstance(warnings, list) else []) + (notes if isinstance(notes, list) else [])
            result['result'] = {**saved, 'warnings': list(dict.fromkeys(value for value in values if isinstance(value, str) and value))}
        return result

    def get(self, ws, job_id: str) -> dict:
        with self.lock:
            row = self._read(ws, job_id)
            return self.public(row)

    def list(self, ws, kind: str | None = None) -> dict:
        if kind not in (None, 'image', 'video'):
            raise ValueError('Choose the image or video studio.')
        with self.lock:
            rows = [self._read(ws, path.stem) for path in directory(ws, '.studio/jobs').glob('*.json')
                    if JOB_ID.fullmatch(path.stem)]
            rows.sort(key=lambda row: row['created'], reverse=True)
            return {'jobs': [self.public(row) for row in rows if kind is None or row['kind'] == kind][:100],
                    'truncated': sum(kind is None or row['kind'] == kind for row in rows) > 100}

    def submit(self, ws, body: dict) -> dict:
        try:
            spec = JobInput.model_validate(body)
        except ValidationError as exc:
            messages = [f"{'.'.join(str(p) for p in error['loc']) or 'input'}: {error['msg']}"
                        for error in exc.errors(include_input=False, include_url=False)]
            raise ValueError('; '.join(messages)[:1500]) from None
        fingerprint = sha256_text(canonical_json(spec.model_dump(mode='json')))
        with self.lock:
            if self.closed:
                raise ValueError('The studio is shutting down. Reopen the workbench before starting a job.')
            for path in directory(ws, '.studio/jobs').glob('*.json'):
                if not JOB_ID.fullmatch(path.stem):
                    continue
                row = self._read(ws, path.stem)
                if row['input']['idempotency_key'] == spec.idempotency_key:
                    if row['fingerprint'] != fingerprint:
                        raise ValueError('This submission key was already used for different settings. Start a new job.')
                    return self.public(row)
            local_matte = spec.kind == 'image' and spec.operation == 'remove_background' and spec.background_method == 'matte'
            if not local_matte and not capabilities()[spec.kind]['available']:
                raise ValueError('Configure the studio provider in Settings before generating.')
            if sum(not item['cancel'].is_set() for item in self.owned.values()) >= 12:
                raise ValueError('The studio queue is full. Wait for a job to finish.')
            job_id = uuid.uuid4().hex
            prepared = prepare(ws, spec, job_id)
            row = {'id': job_id, 'request_id': ws.id, 'request_root': str(ws.root), 'kind': spec.kind,
                   'operation': spec.operation, 'status': 'queued', 'created': now_iso(), 'started': None,
                   'ended': None, 'error': None, 'result': None, 'fingerprint': fingerprint, **prepared,
                   'cancellation_note': 'Stopping the local worker cannot guarantee cancellation or refund at the provider.'}
            write_json(private_job(ws, job_id), row)
            owner = {'ws': ws, 'cancel': threading.Event(), 'process': None}
            self.owned[job_id] = owner
            thread = threading.Thread(target=self._run, args=(ws, job_id, owner), daemon=True, name=f'studio-{job_id[:8]}')
            owner['thread'] = thread
            thread.start()
            return self.public(row)

    @staticmethod
    def _stop(process) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        except ProcessLookupError:
            pass

    def _run(self, ws, job_id: str, owner: dict) -> None:
        with self.semaphore:
            watch_read = watch_write = None
            try:
                with self.lock:
                    row = self._read(ws, job_id)
                    if owner['cancel'].is_set() or self.closed:
                        return
                    row.update(status='running', started=now_iso())
                    write_json(private_job(ws, job_id), row)
                    watch_read, watch_write = os.pipe()
                    process = subprocess.Popen([*self.command, str(private_job(ws, job_id)), '--watch-fd', str(watch_read)],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, env=worker_environment(), start_new_session=True,
                                               pass_fds=(watch_read,))
                    os.close(watch_read)
                    watch_read = None
                    owner['process'] = process
                try:
                    process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self._stop(process)
                with self.lock:
                    row = self._read(ws, job_id)
                    if owner['cancel'].is_set():
                        return
                    result_path = private_job(ws, job_id).with_suffix('.result')
                    result = None if result_path.is_symlink() else read_json(result_path)
                    if process.returncode == 0 and result and result.get('ok'):
                        row.update(status='completed', result=result['result'], ended=now_iso())
                    else:
                        row.update(status='failed', ended=now_iso(), error=(result or {}).get('error') or
                                   'Generation did not finish. Check provider access and try a new job.')
                    write_json(private_job(ws, job_id), row)
                    try:
                        emit(ws, 'studio.completed' if row['status'] == 'completed' else 'studio.failed',
                             {'job_id': job_id, 'kind': row['kind'], 'result': row['result'], 'error': row['error']}, actor='studio')
                    except (OSError, ValueError):
                        # Polling the durable job remains authoritative if the
                        # optional chat activity notification cannot be saved.
                        pass
            except Exception:
                with self.lock:
                    row = self._read(ws, job_id)
                    if not owner['cancel'].is_set():
                        row.update(status='failed', ended=now_iso(), error='The studio worker failed. Start a new job to retry.')
                        write_json(private_job(ws, job_id), row)
            finally:
                for descriptor in (watch_read, watch_write):
                    if descriptor is not None:
                        os.close(descriptor)
                with self.lock:
                    self.owned.pop(job_id, None)

    def cancel(self, ws, job_id: str) -> dict:
        with self.lock:
            row = self._read(ws, job_id)
            if row['status'] in TERMINAL:
                return self.public(row)
            owner = self.owned.get(job_id)
            if owner:
                owner['cancel'].set()
            row.update(status='cancelled', ended=now_iso(), error=None)
            write_json(private_job(ws, job_id), row)
            process = owner['process'] if owner else None
        self._stop(process)
        return self.public(row)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            owners = list(self.owned.items())
        for job_id, owner in owners:
            self.cancel(owner['ws'], job_id)
        for _, owner in owners:
            owner['thread'].join(timeout=5)
