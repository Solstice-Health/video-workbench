"""OpenRouter video discovery and async jobs through the existing model Registry."""
from __future__ import annotations

import re
import time

from filelock import FileLock

from vwb.studio.assets import directory
from vwb.tools import ToolContext, build_registry
from vwb.util.jsonio import read_json, write_json
from vwb.workspace.request import now_iso

TERMINAL_FAILURES = {'failed', 'cancelled', 'expired'}
PENDING = {'pending', 'queued', 'in_progress', 'processing'}


def video_models(ws, query='', limit=50, offset=0, *, registry=None):
    if not isinstance(query, str) or len(query) > 256:
        raise ValueError('Use a model name or path up to 256 characters.')
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
        raise ValueError('Model catalog limit must be 1–100 and offset 0–10000.')
    ctx = ToolContext(ws, 'studio', 'job_lead', None, state={'model_deadline': time.monotonic() + 20})
    result = (registry or build_registry()).call(ctx, 'model_catalog',
        {'provider': 'openrouter', 'modality': 'video', 'query': query, 'limit': limit, 'offset': offset})
    if result.is_error or not isinstance(result.data, dict) or result.data.get('parked'):
        raise ValueError('OpenRouter video catalog is unavailable. Check its connection, account access and model-call policy in Settings.')
    data = result.data
    return {'provider': 'openrouter', 'items': data.get('items', []),
            'total': data.get('total_matching', len(data.get('items', []))), 'offset': offset,
            'next_offset': data.get('next_offset'), 'fetched_at': now_iso(),
            'note': 'Live provider capabilities; model access and reference-image support remain provider-dependent.'}


def validate_model(ws, spec, *, registry=None):
    found, offset = None, 0
    while offset is not None:
        page = video_models(ws, spec.model, limit=100, offset=offset, registry=registry)
        found = next((row for row in page['items'] if row.get('id') == spec.model), None)
        if found:
            break
        next_offset = page['next_offset']
        if next_offset is not None and (type(next_offset) is not int or next_offset <= offset or next_offset > 10000):
            raise ValueError('OpenRouter returned invalid model catalog pagination.')
        offset = next_offset
    if found is None:
        raise ValueError('That exact path is not in the OpenRouter video catalog. Search for a video-generation model.')
    for field, key in [('seconds', 'supported_durations'), ('resolution', 'supported_resolutions'), ('aspect', 'supported_aspect_ratios')]:
        value = getattr(spec, field)
        if value is not None and (not isinstance(found.get(key), list) or value not in found[key]):
            raise ValueError(f'{field} is not advertised for this model. Choose a supported value or use the provider default.')
    if spec.operation == 'frames':
        required = {'first_frame', 'last_frame'} if len(spec.inputs) == 2 else {'first_frame'}
        if not required <= set(found.get('supported_frame_images') or []):
            raise ValueError('This model does not advertise the selected first/last frame inputs.')
    if spec.generate_audio is True and found.get('generate_audio') is not True:
        raise ValueError('This model does not advertise generated audio.')
    if spec.seed is not None and found.get('seed') is not True:
        raise ValueError('This model does not advertise a seed parameter.')
    return {**found, 'checked_at': page['fetched_at']}


def create_args(row):
    spec = row['input']
    payload = {'model': spec['model'], 'prompt': spec['prompt']}
    for key, field in [('duration', 'seconds'), ('resolution', 'resolution'), ('aspect_ratio', 'aspect'),
                       ('generate_audio', 'generate_audio'), ('seed', 'seed')]:
        if spec.get(field) is not None:
            payload[key] = spec[field]
    images = [{'type': 'image_url', 'image_url': {'url': {
        '$file': item['path'].removeprefix('work/'), 'encoding': 'data_url', 'sha256': item['sha256']}}}
        for item in row['prepared']['inputs']]
    if spec['operation'] == 'frames':
        payload['frame_images'] = [image | {'frame_type': 'first_frame' if index == 0 else 'last_frame'}
                                   for index, image in enumerate(images)]
    elif spec['operation'] == 'reference':
        payload['input_references'] = images
    return {'provider': 'openrouter', 'operation': 'video.create', 'payload': payload,
            'purpose': 'Video Studio generation using the explicitly selected model'}


def progress_path(ws, job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('Invalid studio job id.')
    return directory(ws, '.studio/jobs') / f'{job_id}.provider.json'


def read_progress(ws, job_id):
    path = progress_path(ws, job_id)
    if path.is_symlink():
        raise ValueError('Studio provider progress must be a regular file.')
    return read_json(path)


def generate(ctx, row, registry, *, poll_interval=30, timeout=3300):
    """Create once, retain the resource identity, then poll and fetch fixed routes.

    Killing the local worker stops polling, not necessarily the provider job.
    Its saved ID allows explicit inspection without submitting a duplicate.
    """
    from vwb.studio.models import JobInput
    validate_model(ctx.ws, JobInput.model_validate({k: v for k, v in row['input'].items() if k in JobInput.model_fields}), registry=registry)
    path = progress_path(ctx.ws, row['id'])
    deadline = time.monotonic() + timeout
    ctx.state['model_deadline'] = deadline
    progress = {'provider': 'openrouter', 'model': row['input']['model'], 'status': 'dispatching',
                'submitted_at': now_iso(), 'response_paths': []}
    with FileLock(str(path) + '.lock'):
        if path.exists() or path.is_symlink():
            raise RuntimeError('OpenRouter video already has a dispatch record. Inspect that job before starting another; it will not be resubmitted.')
        write_json(path, progress)

    def call(args):
        result = registry.call(ctx, 'model_request', args)
        if result.is_error or not isinstance(result.data, dict) or result.data.get('parked'):
            raise RuntimeError('OpenRouter video request did not complete. Check the saved provider job and account/policy settings before resubmitting.')
        if result.data.get('response_truncated'):
            raise RuntimeError('OpenRouter video response exceeded the studio limit. Inspect its saved response before resubmitting.')
        saved = result.data.get('response_path')
        if isinstance(saved, str):
            # Keep the creation receipt and latest status/content receipts bounded.
            progress['response_paths'] = (progress['response_paths'][:1] + [saved])[-2:]
        return result

    def response(result):
        native = result.data.get('response')
        if not isinstance(native, dict) or not isinstance(native.get('status'), str):
            raise RuntimeError('OpenRouter video returned an invalid status response. Inspect its saved response before resubmitting.')
        return native

    try:
        created = call(create_args(row))
        native = response(created)
        job_id = native.get('id')
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,250}', job_id) or job_id in {'.', '..'}:
            raise RuntimeError('OpenRouter video returned no valid job ID. Inspect the saved model response before resubmitting.')
        progress.update(id=job_id, status=native.get('status', 'pending'), updated_at=now_iso())
        write_json(path, progress)
        while True:
            status = native.get('status')
            if status == 'completed':
                break
            if status in TERMINAL_FAILURES:
                raise RuntimeError(f'OpenRouter video job {status}. Its provider ID is saved for inspection; no retry was submitted.')
            if status not in PENDING:
                raise RuntimeError('OpenRouter video returned an unknown status. Inspect the saved job before resubmitting.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('OpenRouter video timed out locally. Its provider job may still be running; inspect the saved ID before resubmitting.')
            time.sleep(min(poll_interval, remaining))
            polled = call({'provider': 'openrouter', 'operation': 'video.status', 'payload': {'id': job_id},
                           'purpose': 'Poll the existing Video Studio generation'})
            native = response(polled)
            if native.get('id') not in {None, job_id}:
                raise RuntimeError('OpenRouter video status returned a different job ID.')
            progress.update(status=native.get('status'), updated_at=now_iso())
            write_json(path, progress)
        # Never follow provider-supplied polling/download URLs with credentials.
        result = call({'provider': 'openrouter', 'operation': 'video.content', 'payload': {'id': job_id, 'index': 0},
                       'purpose': 'Download the completed Video Studio output'})
        progress.update(status='completed', downloaded_at=now_iso())
        write_json(path, progress)
        result.data.update(model=row['input']['model'], provider_job=dict(progress))
        return result
    except Exception:
        progress['last_local_error_at'] = now_iso()
        write_json(path, progress)
        raise
