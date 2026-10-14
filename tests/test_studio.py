"""Studio boundary and job tests use local media and stub workers, never paid APIs."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from vwb.gateway.core import SlotCall
from vwb.gen import images
from vwb.studio.assets import publish, snapshot_input, upload
from vwb.studio.models import Input, JobInput, StudioGateway, capabilities
from vwb.studio.service import StudioJobs, prepare
from vwb.studio.worker import execute, tool_call
from vwb.tools import gen_tools
from vwb.tools.base import Registry, Tool, ToolResult
from vwb.util.hashing import sha256_file
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry as AssetRegistry
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / 'r-studio'
    for part in SUBDIRS:
        (root / part).mkdir(parents=True)
    ws = Workspace(root)
    ws.save(Request(id='r-studio', message='Studio fixture'))
    return ws


def spec(**changes):
    return {'kind': 'image', 'operation': 'create', 'prompt': 'A blue sphere',
            'idempotency_key': 'submission-key-0001', **changes}


def png(size=(64, 64), color=(1, 2, 3, 255)):
    buf = io.BytesIO()
    Image.new('RGBA', size, color).save(buf, format='PNG')
    return buf.getvalue()


def put(ws, *, kind='image', data=None):
    data = data or png()
    return upload(ws, io.BytesIO(data), len(data), name='test.png', kind=kind)


def until(fn, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.02)
    pytest.fail('Studio worker timed out')


def test_catalog_is_configured_models_and_selection_is_pinned(ws, monkeypatch):
    monkeypatch.setattr('vwb.studio.models.provider_status', lambda: [{'id': 'openai', 'configured': True}])
    catalog = capabilities()
    assert catalog['image']['available'] and not catalog['video']['available']
    assert catalog['image']['default_background_method'] == 'matte'
    assert catalog['image']['background_methods'][0]['available']
    for item in catalog['image']['models']:
        gw = StudioGateway(ws, kind='image', model=item['id'])
        assert gw.resolve_slot('img.sunburst')['model'] == item['id']
        assert gw.resolve_slot('img.sunburst')['fallbacks'] == []
    with pytest.raises(ValueError):
        StudioGateway(ws, kind='video', model='imaginary-provider-model')


@pytest.mark.parametrize('changes', [
    {'model': 'anything'}, {'size': [0, 200]}, {'size': [10**9, 1024]}, {'size': [100, 1024]},
    {'seconds': 30}, {'operation': 'mystery'}, {'operation': 'edit'}, {'seed': True},
    {'kind': 'video', 'operation': 'frames'}, {'kind': 'video', 'operation': 'text', 'inputs': [{'path': 'work/a.png'}]},
    {'mask': {'path': 'work/mask.png'}}, {'operation': 'inpaint', 'inputs': [{'path': 'work/a.png'}]},
    {'surprise': 'no'}, {'place': [0, 0, 10, 10]},
])
def test_invalid_controls_rejected(changes):
    with pytest.raises(ValidationError):
        JobInput.model_validate(spec(**changes))


def test_upload_normalizes_and_snapshot_pins_bytes(ws):
    item = put(ws)
    assert item['path'].startswith('work/studio/inputs/') and item['mime'] == 'image/png'
    request = JobInput.model_validate(spec(operation='edit', inputs=[{'upload_id': item['upload_id']}]))
    prepared = prepare(ws, request, 'a' * 32)
    snap = prepared['prepared']['inputs'][0]
    original = (ws.root / snap['path']).read_bytes()
    (ws.root / item['path']).write_bytes(png(color=(200, 0, 0, 255)))
    assert (ws.root / snap['path']).read_bytes() == original
    assert AssetRegistry(ws.assets).get(snap['ref'])['sha256'] == snap['sha256']


def test_normalized_workspace_image_retains_original_source_digest(ws):
    source = ws.work / 'photo.jpg'
    Image.new('RGB', (64, 64), (30, 80, 130)).save(source, format='JPEG')
    request = JobInput.model_validate(spec(operation='edit', inputs=[{'path': 'work/photo.jpg'}]))
    item = prepare(ws, request, 'a' * 32)['prepared']['inputs'][0]
    lineage = json.loads(AssetRegistry(ws.assets).get(item['ref'])['derived_from'])[0]
    assert lineage['path'] == 'work/photo.jpg'
    assert lineage['sha256'] == item['source_sha256'] == sha256_file(source)
    assert item['sha256'] == sha256_file(ws.root / item['path'])
    assert item['sha256'] != item['source_sha256']


def test_workspace_private_and_symlink_inputs_rejected(ws, tmp_path):
    outside = tmp_path / 'outside.png'
    outside.write_bytes(png())
    (ws.work / 'escape.png').symlink_to(outside)
    folder = ws.work / 'snapshot'
    folder.mkdir()
    for path in ('../outside.png', 'memory/token.png', 'work/escape.png'):
        with pytest.raises((ValueError, PermissionError)):
            snapshot_input(ws, Input(path=path), folder, '0', kind='image')
    (ws.work / 'studio').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='symbolic'):
        put(ws)


def test_mask_alpha_and_geometry_are_enforced(ws):
    image = put(ws)
    with pytest.raises(ValueError, match='empty'):
        put(ws, kind='mask', data=png(color=(255, 255, 255, 0)))
    opaque_rgb = io.BytesIO()
    Image.new('RGB', (64, 64), 'black').save(opaque_rgb, format='PNG')
    with pytest.raises(ValueError, match='alpha'):
        put(ws, kind='mask', data=opaque_rgb.getvalue())
    wrong = put(ws, kind='mask', data=png(size=(32, 32)))
    request = JobInput.model_validate(spec(operation='inpaint', inputs=[{'upload_id': image['upload_id']}],
                                         mask={'upload_id': wrong['upload_id']}))
    with pytest.raises(ValueError, match='dimensions'):
        prepare(ws, request, 'a' * 32)
    mask = put(ws, kind='mask')
    request.mask = Input(upload_id=mask['upload_id'])
    prepared = prepare(ws, request, 'b' * 32)
    row = {'id': 'b' * 32, **prepared}
    name, args = tool_call(row)
    assert name == 'sunburst_inpaint' and args['mask']['asset'].startswith('slot:studio-input-')
    assert row['input']['effective_size'] == [64, 64]


def test_targeted_extraction_and_outpaint_validation(ws):
    image = put(ws)
    request = JobInput.model_validate(spec(operation='remove_background', inputs=[{'upload_id': image['upload_id']}],
                                         subject='blue shape', box=[4, 5, 20, 30], background_method='sunburst'))
    row = {'id': 'c' * 32, **prepare(ws, request, 'c' * 32)}
    name, args = tool_call(row)
    assert name == 'sunburst_extract' and args['box'] == [4, 5, 20, 30] and args['subject'] == 'blue shape'
    assert args['method'] == 'sunburst'
    request = JobInput.model_validate(spec(operation='outpaint', inputs=[{'upload_id': image['upload_id']}],
                                         place=[-1, 2, 100, 100]))
    with pytest.raises(ValueError, match='inside'):
        prepare(ws, request, 'd' * 32)


def test_inpaint_preserves_unselected_pixels_without_provider(monkeypatch):
    original = np.full((100, 100, 4), [50, 70, 90, 255], dtype=np.uint8)
    mask = np.zeros((100, 100), dtype=np.uint8)
    mask[45:55, 45:55] = 255
    monkeypatch.setattr(images, '_call', lambda *a, **kw: SlotCall(slot='img.sunburst', provider='openai', model='test',
                       text='', raw=[np.full((1024, 1024, 4), 240, dtype=np.uint8)]))
    monkeypatch.setattr(images.qa, 'read_text', lambda image: [])
    result = images.inpaint(None, original, mask, 'Change selected region', exact_mask=True)
    assert np.array_equal(result['rgb'][mask == 0], original[..., :3][mask == 0])
    assert not np.array_equal(result['rgb'][48:52, 48:52], original[48:52, 48:52, :3])


def test_execute_crosses_registry_and_saves_provenance(ws):
    row = {'id': 'e' * 32, 'kind': 'image', 'operation': 'create', 'request_root': str(ws.root),
           **prepare(ws, JobInput.model_validate(spec()), 'e' * 32)}
    calls = []
    def handler(ctx, args):
        calls.append((ctx.role, args))
        path = ws.work / 'mock.png'
        path.write_bytes(png())
        ref = AssetRegistry(ws.assets).register(path, slot=args['slot'], kind='image', receipt='sunburst.create', derived_from=[])
        return ToolResult.json({'model': row['input']['model'], 'assets': [{'ref': ref}],
                                'warnings': ['Generated text needs review'],
                                'notes': ['Seam needs review', 'Generated text needs review']}, artifacts=[ref])
    registry = Registry([Tool('sunburst_create', 'test', {}, handler, capability='gen.image')])
    result = execute(row, registry=registry, gateway=object())
    assert calls[0][0] == 'job_lead'
    output = result['artifacts'][0]
    assert output['path'].startswith('work/studio/images/') and output['provenance'] == 'generated'
    receipt = read_json(ws.root / result['receipt_path'])
    assert receipt['artifacts'][0]['sha256'] == output['sha256']
    assert 'sunburst_create' in (ws.ledger / 'tools.jsonl').read_text()
    assert result['warnings'] == ['Generated text needs review', 'Seam needs review']


def test_policy_block_prevents_generation(ws, monkeypatch):
    row = {'id': 'e' * 32, 'kind': 'image', 'operation': 'create', 'request_root': str(ws.root),
           **prepare(ws, JobInput.model_validate(spec()), 'e' * 32)}
    called = []
    registry = Registry([Tool('sunburst_create', 'test', {}, lambda *a: called.append(True), capability='gen.image')])
    monkeypatch.setattr(registry, 'mode', lambda name: 'block')
    with pytest.raises(RuntimeError):
        execute(row, registry=registry, gateway=object())
    assert not called


def test_historical_quality_notes_are_visible_without_rewriting_receipt():
    row = {'result': {'warnings': [], 'tool_result': {'notes': ['Seam needs review']}}}
    assert StudioJobs.public(row)['result']['warnings'] == ['Seam needs review']
    assert row['result']['warnings'] == []


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr('vwb.studio.service.capabilities', lambda: {'image': {'available': True}, 'video': {'available': True}})
    script = tmp_path / 'worker.py'
    script.write_text('''import json, pathlib, sys, time
p = pathlib.Path(sys.argv[1]); row=json.loads(p.read_text())
pathlib.Path(row['request_root'], 'started-'+row['id']).write_text(str(__import__('os').getpid()))
time.sleep(30 if row['input']['prompt']=='hold' else .1)
p.with_suffix('.result').write_text(json.dumps({'ok':True,'result':{'artifacts':[]}}))
''')
    return [sys.executable, str(script)]


def test_idempotent_jobs_history_and_no_restart_replay(ws, worker):
    jobs = StudioJobs(command=worker)
    try:
        first = jobs.submit(ws, spec())
        duplicate = jobs.submit(ws, spec())
        assert first['id'] == duplicate['id']
        with pytest.raises(ValueError, match='different settings'):
            jobs.submit(ws, spec(prompt='different'))
        until(lambda: jobs.get(ws, first['id'])['status'] == 'completed')
        assert len(jobs.list(ws)['jobs']) == 1
    finally:
        jobs.close()
    restarted = StudioJobs(command=worker)
    try:
        assert restarted.get(ws, first['id'])['status'] == 'completed'
        assert restarted.submit(ws, spec())['id'] == first['id']
        path = ws.root / '.studio/jobs' / f"{first['id']}.json"
        state = read_json(path)
        state['status'] = 'running'
        write_json(path, state)
        assert restarted.get(ws, first['id'])['status'] == 'interrupted'
        assert restarted.submit(ws, spec())['status'] == 'interrupted'
    finally:
        restarted.close()


def test_cancel_kills_owned_process_and_queued_job_never_starts(ws, worker):
    jobs = StudioJobs(command=worker, concurrency=1)
    try:
        first = jobs.submit(ws, spec(prompt='hold'))
        marker = ws.root / ('started-' + first['id'])
        until(marker.exists)
        pid = int(marker.read_text())
        second = jobs.submit(ws, spec(idempotency_key='submission-key-0002'))
        jobs.cancel(ws, second['id'])
        assert jobs.cancel(ws, first['id'])['status'] == 'cancelled'
        until(lambda: not jobs.owned)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not (ws.root / ('started-' + second['id'])).exists()
    finally:
        jobs.close()


def test_receipt_tampering_is_rejected_before_provenance_inheritance(ws):
    item = put(ws)
    (ws.root / item['path']).write_bytes(png(color=(200, 0, 0, 255)))
    request = JobInput.model_validate(spec(operation='edit', inputs=[{'upload_id': item['upload_id']}]))
    with pytest.raises(ValueError, match='changed after its receipt'):
        prepare(ws, request, 'f' * 32)
    clean = put(ws)
    request.inputs = [Input(upload_id=clean['upload_id'])]
    prepared = prepare(ws, request, 'a' * 32)
    ref = prepared['prepared']['inputs'][0]['ref']
    asset = AssetRegistry(ws.assets).get(ref)
    Path(asset['path']).write_bytes(png(color=(0, 200, 0, 255)))
    request.inputs = [Input(ref=ref)]
    with pytest.raises(ValueError, match='changed after its receipt'):
        prepare(ws, request, 'b' * 32)


def test_publication_refuses_symlink_parents_and_final_targets(ws, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (ws.work / 'redirect').symlink_to(outside)
    with pytest.raises(ValueError):
        publish(ws, 'work/redirect/output.png', b'no')
    (ws.work / 'target.png').symlink_to(outside / 'output.png')
    with pytest.raises(FileExistsError):
        publish(ws, 'work/target.png', b'no')
    assert not (outside / 'output.png').exists()


def test_parent_pipe_closure_stops_actual_watchdog_without_replay(tmp_path):
    read_fd, write_fd = os.pipe()
    script = 'from vwb.studio.worker import watch_parent; import sys,time; watch_parent(int(sys.argv[1])); print("ready",flush=True); time.sleep(60)'
    process = subprocess.Popen([sys.executable, '-c', script, str(read_fd)], pass_fds=(read_fd,),
                               start_new_session=True, stdout=subprocess.PIPE, text=True)
    os.close(read_fd)
    try:
        assert process.stdout.readline().strip() == 'ready'
        os.close(write_fd)
        write_fd = None
        assert process.wait(timeout=5) == -15
    finally:
        if write_fd is not None:
            os.close(write_fd)
        if process.poll() is None:
            process.kill()
            process.wait()


def test_deadline_stops_hung_worker(ws, worker):
    jobs = StudioJobs(command=worker, timeout=.2)
    try:
        row = jobs.submit(ws, spec(prompt='hold'))
        until(lambda: jobs.get(ws, row['id'])['status'] == 'failed')
        pid = int((ws.root / ('started-' + row['id'])).read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        jobs.close()


def test_local_matte_preserves_edge_alpha_and_builds_full_source_mask(ws, monkeypatch):
    # Non-square source, foreground touches the left edge. A default inset crop
    # would lose that foreground even though the UI selected the whole image.
    rgba = np.zeros((80, 192, 4), np.uint8)
    rgba[..., :3] = [25, 70, 120]
    rgba[20:60, :36, 3] = 255
    rgba[22:58, 36:39, 3] = 100
    source = ws.work / 'transparent.png'
    Image.fromarray(rgba).save(source)
    def forbidden(*args, **kwargs):
        pytest.fail('Local matte must not call a provider')
    monkeypatch.setattr('vwb.gateway.core.Gateway.openai', forbidden)
    monkeypatch.setattr('vwb.gateway.core.Gateway.google', forbidden)
    monkeypatch.setattr(gen_tools.qa, 'faces', lambda image: 0)
    spec_ = JobInput.model_validate(spec(operation='remove_background', inputs=[{'path': 'work/transparent.png'}],
                                        prompt='', subject=''))
    assert spec_.background_method == 'matte' and spec_.model is None
    row = {'id': 'f' * 32, 'kind': 'image', 'operation': 'remove_background', 'request_root': str(ws.root),
           **prepare(ws, spec_, 'f' * 32)}
    assert row['input']['box'] == [0, 0, 192, 80]
    result = execute(row, registry=Registry(gen_tools.TOOLS))
    assert result['model'] == 'local-matte' and len(result['artifacts']) == 1
    mask = result['background_mask']
    assert (mask['width'], mask['height']) == (192, 80)
    assert mask['receipt'] == 'matte.background_mask' and mask['provenance'] == 'segmented'
    with Image.open(ws.root / mask['path']) as image:
        assert np.array_equal(np.asarray(image)[..., 3], 255 - rgba[..., 3])
    x, y, w, h = result['tool_result']['placed_in_source']
    with Image.open(ws.root / result['artifacts'][0]['path']) as image:
        assert np.array_equal(np.asarray(image), rgba[y:y+h, x:x+w])
    assert mask['source_ref'] == row['prepared']['inputs'][0]['ref']
    assert AssetRegistry(ws.assets).get(mask['ref'])['sha256'] == mask['sha256']
    assert read_json(ws.root / result['receipt_path'])['background_mask']['ref'] == mask['ref']


def test_local_matte_mask_reconstructs_off_origin_selected_region(ws, monkeypatch):
    monkeypatch.setattr(gen_tools.qa, 'faces', lambda image: 0)
    rgba = np.zeros((80, 120, 4), np.uint8)
    rgba[..., :3] = [40, 90, 150]
    rgba[20:40, 50:70, 3] = 255
    rgba[5:10, 5:10, 3] = 255  # another object outside the selected subject region
    Image.fromarray(rgba).save(ws.work / 'source.png')
    spec_ = JobInput.model_validate(spec(operation='remove_background', inputs=[{'path': 'work/source.png'}],
                                        box=[48, 18, 24, 24]))
    row = {'id': 'a' * 32, 'kind': 'image', 'operation': 'remove_background', 'request_root': str(ws.root),
           **prepare(ws, spec_, 'a' * 32)}
    result = execute(row, registry=Registry(gen_tools.TOOLS), gateway=object())
    with Image.open(ws.root / result['background_mask']['path']) as image:
        alpha = np.asarray(image)[..., 3]
    expected = np.full((80, 120), 255, np.uint8)
    expected[20:40, 50:70] = 0
    assert np.array_equal(alpha, expected)


def test_local_matte_full_opaque_region_has_safe_grabcut_seed(monkeypatch):
    monkeypatch.setattr('vwb.index.layout.background_color', lambda region, band: (np.zeros(3), 0.0))
    image = np.full((40, 60, 4), 255, np.uint8)
    image[10:30, 20:40, :3] = [20, 40, 80]
    result = images.matte_extract(image, [0, 0, 60, 40], preserve_rgb=True)
    assert result['method'] == 'grabcut'
    x, y, w, h = result['placed']
    assert np.array_equal(result['rgba'][..., :3], image[y:y+h, x:x+w, :3])


def test_local_matte_submission_does_not_require_credentials(ws, worker, monkeypatch):
    monkeypatch.setattr('vwb.studio.service.capabilities', lambda: {'image': {'available': False}})
    image = put(ws)
    jobs = StudioJobs(command=worker)
    try:
        row = jobs.submit(ws, spec(operation='remove_background', prompt='', subject='',
                                   inputs=[{'upload_id': image['upload_id']}]))
        assert row['input']['background_method'] == 'matte' and row['input']['model'] is None
        until(lambda: jobs.get(ws, row['id'])['status'] == 'completed')
        with pytest.raises(ValueError, match='provider'):
            jobs.submit(ws, spec(operation='remove_background', background_method='sunburst', subject='subject',
                                 inputs=[{'upload_id': image['upload_id']}], idempotency_key='new-local-key-0002'))
    finally:
        jobs.close()
