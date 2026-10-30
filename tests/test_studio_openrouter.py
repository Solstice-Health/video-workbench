"""OpenRouter Studio uses live metadata, Registry policy and one paid dispatch."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from vwb.gateway import model_access
from vwb.studio import openrouter
from vwb.studio.models import JobInput, capabilities
from vwb.studio.service import StudioJobs, prepare, private_job, worker_environment
from vwb.studio.worker import execute, tool_call
from vwb.tools import build_registry
from vwb.tools.base import Registry, ToolContext
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import canonical_json, read_json, write_json
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry as Assets
from vwb.workspace.request import SUBDIRS, Request

H3 = {'id': 'minimax/hailuo-3', 'name': 'MiniMax: H3', 'supported_durations': list(range(5, 16)),
      'supported_resolutions': ['2K'], 'supported_aspect_ratios': ['16:9', '9:16', '1:1'],
      'supported_frame_images': ['first_frame', 'last_frame'], 'generate_audio': True, 'seed': False}
SECRET = 'fixture-key-never-return'


@pytest.fixture
def ws(tmp_path):
    ws = Workspace(tmp_path / 'r-router')
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message='Isolated OpenRouter test'))
    return ws


def spec(**changes):
    return JobInput.model_validate({'idempotency_key': 'openrouter-fixture-0001', 'kind': 'video', 'provider': 'openrouter',
        'model': H3['id'], 'operation': 'text', 'prompt': 'A blue sphere', 'resolution': '2K', 'seconds': 5,
        'aspect': '16:9', **changes})


def row(ws, **changes):
    selected = spec(**changes)
    return {'id': 'a' * 32, 'kind': 'video', 'operation': selected.operation, 'request_root': str(ws.root),
            **prepare(ws, selected, 'a' * 32)}


@pytest.fixture
def intercept(monkeypatch):
    build_registry()  # Import provider SDK classes before replacing the HTTP constructor.
    original = httpx.Client
    calls = []
    monkeypatch.setattr(model_access, 'secrets', lambda: SimpleNamespace(require=lambda provider: SECRET))
    def install(handler):
        def wrapped(request):
            assert request.url.host == 'openrouter.ai'
            assert request.headers['authorization'] == 'Bearer ' + SECRET
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(model_access.httpx, 'Client', lambda **kw: original(**kw, transport=httpx.MockTransport(wrapped)))
        return calls
    return install


def reply(value, code=200):
    return httpx.Response(code, json=value)


def test_capabilities_expose_both_routes_and_do_not_require_google(monkeypatch):
    monkeypatch.setattr('vwb.studio.models.provider_status', lambda: [{'id': 'openrouter', 'configured': True}])
    video = capabilities()['video']
    assert video['available'] and video['provider'] == 'openrouter'
    assert video['providers'][1]['default_model'] == H3['id']
    assert not video['providers'][0]['available']
    assert video['providers'][1]['custom_model']


@pytest.mark.parametrize('changes', [
    {'model': 'https://evil.test/model'}, {'model': 'minimax/../model'}, {'model': 'h3'},
    {'operation': 'edit', 'inputs': [{'path': 'work/in.mp4'}]}, {'provider': 'openai'},
    {'kind': 'image', 'operation': 'create'}, {'seconds': True}, {'seconds': 10000},
])
def test_invalid_provider_or_path_never_dispatches(changes):
    with pytest.raises(ValidationError):
        spec(**changes)


def test_live_search_preserves_capabilities_and_exact_custom_id(ws, intercept):
    custom = {**H3, 'id': 'another/new-video-v9', 'name': 'Future video'}
    calls = intercept(lambda req: reply({'data': [H3, custom]}))
    result = openrouter.video_models(ws, 'future')
    assert result['items'] == [custom] and result['total'] == 1
    assert calls[0].url.path == '/api/v1/videos/models'
    selected = openrouter.validate_model(ws, spec(model=custom['id']))
    assert selected['id'] == custom['id'] and selected['checked_at']
    assert '2K' in selected['supported_resolutions']


@pytest.mark.parametrize('changes,phrase', [
    ({'resolution': '1080p'}, 'resolution'), ({'seconds': 4}, 'seconds'),
    ({'aspect': '4:1'}, 'aspect'), ({'seed': 1}, 'seed'), ({'model': 'other/text-only'}, 'exact path'),
])
def test_live_capability_mismatches_fail_before_paid_request(ws, intercept, changes, phrase):
    calls = intercept(lambda req: reply({'data': [H3]}))
    with pytest.raises(ValueError, match=phrase):
        openrouter.validate_model(ws, spec(**changes))
    assert all(c.method == 'GET' and c.url.path.endswith('/models') for c in calls)


def test_provider_defaults_do_not_invent_supported_settings(ws, intercept):
    intercept(lambda req: reply({'data': [{**H3, 'supported_durations': None, 'supported_resolutions': [], 'supported_aspect_ratios': None}]}))
    selected = spec(seconds=None, resolution=None, aspect=None)
    assert openrouter.validate_model(ws, selected)['id'] == H3['id']
    payload = tool_call(row(ws, seconds=None, resolution=None, aspect=None))[1]['payload']
    assert set(payload) == {'model', 'prompt'}


def test_first_last_and_reference_inputs_use_pinned_workspace_markers(ws, intercept):
    Image.new('RGB', (64, 64), 'blue').save(ws.work / 'frame.png')
    images = [{'path': 'work/frame.png'}, {'path': 'work/frame.png'}]
    job = row(ws, operation='frames', inputs=images)
    payload = tool_call(job)[1]['payload']
    assert [p['frame_type'] for p in payload['frame_images']] == ['first_frame', 'last_frame']
    marker = payload['frame_images'][0]['image_url']['url']
    assert marker['sha256'] == hashlib.sha256((ws.work / marker['$file']).read_bytes()).hexdigest()
    assert marker['encoding'] == 'data_url'
    (ws.work / marker['$file']).write_bytes(b'changed')
    calls = intercept(lambda req: pytest.fail('Changed reference was sent to provider'))
    result = build_registry().call(ToolContext(ws, 'studio', 'job_lead', None), 'model_request', tool_call(job)[1])
    assert result.is_error and 'SHA-256' in result.text and not calls


def test_frame_and_audio_controls_require_advertised_support(ws, intercept):
    intercept(lambda req: reply({'data': [{**H3, 'supported_frame_images': [], 'generate_audio': False}]}))
    with pytest.raises(ValueError, match='frame inputs'):
        openrouter.validate_model(ws, spec(operation='frames', inputs=[{'path': 'work/anything.png'}]))
    with pytest.raises(ValueError, match='audio'):
        openrouter.validate_model(ws, spec(generate_audio=True))
    assert openrouter.validate_model(ws, spec(generate_audio=False))


def test_real_movie_download_is_registered_published_and_receipted_once(ws, intercept, monkeypatch):
    path = ws.work / 'fixture.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=10',
                    '-t', '0.3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True)
    binary = path.read_bytes()
    def handler(req):
        if req.url.path.endswith('/models'):
            return reply({'data': [H3]})
        if req.method == 'POST':
            body = json.loads(req.content)
            assert body['model'] == H3['id'] and body['resolution'] == '2K'
            return reply({'id': 'video-123', 'status': 'pending', 'polling_url': 'https://evil.invalid/poll'}, 202)
        if req.url.path.endswith('/content'):
            return httpx.Response(200, content=binary, headers={'content-type': 'video/mp4'})
        return reply({'id': 'video-123', 'status': 'completed', 'unsigned_urls': ['https://evil.invalid/content']})
    calls = intercept(handler)
    original = openrouter.generate
    monkeypatch.setattr(openrouter, 'generate', lambda ctx, job, registry: original(ctx, job, registry, poll_interval=0))
    job = row(ws)
    result = execute(job)
    assert [c.method for c in calls].count('POST') == 1
    assert calls[-1].url.path == '/api/v1/videos/video-123/content' and calls[-1].url.params['index'] == '0'
    artifact = result['artifacts'][0]
    assert (ws.root / artifact['path']).read_bytes() == binary
    assert artifact['frames'] == 3 and artifact['provenance'] == 'generated'
    assert Assets(ws.assets).get(artifact['ref'])['sha256'] == artifact['sha256']
    receipt = read_json(ws.root / result['receipt_path'])
    assert receipt['requested']['provider'] == 'openrouter' and receipt['requested']['model'] == H3['id']
    assert receipt['tool_result']['provider_job']['id'] == 'video-123'
    assert SECRET not in json.dumps(result)
    assert 'evil.invalid' not in json.dumps(openrouter.read_progress(ws, job['id']))
    with pytest.raises(RuntimeError, match='already has a dispatch'):
        execute(job)
    assert [c.method for c in calls].count('POST') == 1


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'expired', 'surprise'])
def test_terminal_or_unknown_status_retains_provider_id_without_retry(ws, intercept, status):
    def handler(req):
        if req.url.path.endswith('/models'):
            return reply({'data': [H3]})
        if req.method == 'POST':
            return reply({'id': 'video-terminal', 'status': status}, 202)
        pytest.fail('Failed job should not download')
    calls = intercept(handler)
    job = row(ws)
    with pytest.raises(RuntimeError, match='OpenRouter video'):
        openrouter.generate(ToolContext(ws, 'studio', 'job_lead', None), job, build_registry(), poll_interval=0)
    progress = openrouter.read_progress(ws, job['id'])
    assert progress['id'] == 'video-terminal' and progress['status'] == status
    assert len([c for c in calls if c.method == 'POST']) == 1


def test_generation_policy_blocks_paid_post(ws, intercept, monkeypatch):
    calls = intercept(lambda req: reply({'data': [H3]}))
    normal = Registry.mode
    monkeypatch.setattr(Registry, 'mode', staticmethod(lambda cap: 'block' if cap == 'gen.video' else normal(cap)))
    with pytest.raises(RuntimeError, match='request did not complete'):
        execute(row(ws))
    assert all(c.method == 'GET' for c in calls)


def test_provider_progress_survives_server_restart_and_is_exposed_without_replay(ws):
    job = row(ws)
    job.update(status='running', fingerprint='old', created='2026-10-30T00:00:00Z')
    write_json(private_job(ws, job['id']), job)
    write_json(openrouter.progress_path(ws, job['id']), {'id': 'upstream-job', 'status': 'in_progress'})
    jobs = StudioJobs()
    try:
        result = jobs.get(ws, job['id'])
        assert result['status'] == 'interrupted' and result['provider_job']['id'] == 'upstream-job'
        assert not jobs.owned
    finally:
        jobs.close()


def test_idempotent_submit_does_not_rediscover_or_dispatch_twice(ws, intercept, monkeypatch, tmp_path):
    calls = intercept(lambda req: reply({'data': [H3]}))
    monkeypatch.setattr('vwb.studio.service.capabilities', lambda: {'video': {'available': True, 'providers': [{'id': 'openrouter', 'available': True}]}})
    worker = tmp_path / 'worker.py'
    worker.write_text("import pathlib,json,sys; p=pathlib.Path(sys.argv[1]); p.with_suffix('.result').write_text(json.dumps({'ok':True,'result':{'artifacts':[]}}))")
    jobs = StudioJobs(command=[sys.executable, str(worker)])
    try:
        first = jobs.submit(ws, spec().model_dump())
        assert first['model_capabilities']['id'] == H3['id']
        before = len(calls)
        assert jobs.submit(ws, spec().model_dump())['id'] == first['id']
        assert len(calls) == before
        with pytest.raises(ValueError, match='different settings'):
            jobs.submit(ws, spec(seconds=6).model_dump())
    finally:
        jobs.close()


def test_legacy_pending_submission_retains_idempotency():
    selected = JobInput.model_validate({'idempotency_key': 'older-ui-fixture-0001', 'kind': 'image', 'operation': 'create', 'prompt': 'Blue'})
    legacy = selected.model_dump()
    legacy.pop('provider')
    legacy.pop('generate_audio')
    fingerprint = sha256_text(canonical_json(selected.model_dump()))
    assert StudioJobs._same_submission({'input': legacy, 'fingerprint': sha256_text(canonical_json(legacy))}, fingerprint)
    legacy['prompt'] = 'Different'
    assert not StudioJobs._same_submission({'input': legacy, 'fingerprint': 'old'}, fingerprint)


def test_worker_credentials_stay_in_host_environment(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', SECRET)
    monkeypatch.setenv('UNRELATED_PRIVATE_KEY', 'do-not-inherit')
    env = worker_environment()
    assert env['OPENROUTER_API_KEY'] == SECRET and 'UNRELATED_PRIVATE_KEY' not in env


@pytest.mark.parametrize('response', [[], {'id': 'a', 'status': []}, {'id': '../bad', 'status': 'pending'}])
def test_malformed_creation_response_does_not_replay(ws, intercept, response):
    calls = intercept(lambda req: reply({'data': [H3]} if req.method == 'GET' else response))
    job = row(ws)
    with pytest.raises(RuntimeError, match='OpenRouter video'):
        execute(job)
    with pytest.raises(RuntimeError, match='already has a dispatch'):
        execute(job)
    assert sum(req.method == 'POST' for req in calls) == 1
    assert openrouter.read_progress(ws, job['id'])['last_local_error_at']


def test_failed_download_retains_completed_provider_id_and_never_resubmits(ws, intercept):
    def handler(req):
        if req.url.path.endswith('/models'):
            return reply({'data': [H3]})
        if req.method == 'POST':
            return reply({'id': 'completed-job', 'status': 'completed'}, 202)
        return reply({'error': 'temporary download failure'}, 503)
    calls = intercept(handler)
    job = row(ws)
    with pytest.raises(RuntimeError, match='request did not complete'):
        execute(job)
    saved = openrouter.read_progress(ws, job['id'])
    assert saved['id'] == 'completed-job' and saved['status'] == 'completed'
    assert 'downloaded_at' not in saved
    with pytest.raises(RuntimeError, match='already has a dispatch'):
        execute(job)
    assert sum(req.method == 'POST' for req in calls) == 1


@pytest.mark.parametrize('digest', ['bad', 'a' * 64, 17])
def test_file_marker_invalid_hash_fails_before_network(ws, intercept, digest):
    (ws.work / 'input.txt').write_text('pinned input')
    calls = intercept(lambda req: pytest.fail('Invalid pin reached network'))
    args = {'provider': 'openrouter', 'operation': 'chat.completions', 'payload': {'model': 'vendor/example',
            'messages': [{'role': 'user', 'content': {'$file': 'input.txt', 'encoding': 'text', 'sha256': digest}}]}}
    result = build_registry().call(ToolContext(ws, 'studio', 'job_lead', None), 'model_request', args)
    assert result.is_error and 'SHA-256' in result.text and not calls


def test_matching_file_marker_hash_expands_before_network(ws, intercept):
    raw = b'pinned input'
    (ws.work / 'input.txt').write_bytes(raw)
    calls = intercept(lambda req: reply({'choices': []}))
    args = {'provider': 'openrouter', 'operation': 'chat.completions', 'payload': {'model': 'vendor/example',
            'messages': [{'role': 'user', 'content': {'$file': 'input.txt', 'encoding': 'text', 'sha256': hashlib.sha256(raw).hexdigest()}}]}}
    result = build_registry().call(ToolContext(ws, 'studio', 'job_lead', None), 'model_request', args)
    assert not result.is_error, result.text
    assert json.loads(calls[0].content)['messages'][0]['content'] == 'pinned input'


def test_concurrent_workers_share_one_dispatch_record(ws, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from vwb.tools.base import ToolResult

    ready = threading.Barrier(2)
    calls = []
    def validate(*args, **kwargs):
        ready.wait(timeout=5)
        return H3
    monkeypatch.setattr(openrouter, 'validate_model', validate)
    class FakeRegistry:
        def call(self, ctx, name, args):
            calls.append(args['operation'])
            return ToolResult.json({'response': {'id': 'one-job', 'status': 'completed'}})
    job = row(ws)
    def run():
        try:
            openrouter.generate(ToolContext(ws, 'studio', 'job_lead', None), job, FakeRegistry())
            return 'completed'
        except RuntimeError as error:
            assert 'already has a dispatch' in str(error)
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: run(), range(2))) == ['completed', 'refused']
    assert calls == ['video.create', 'video.content']
