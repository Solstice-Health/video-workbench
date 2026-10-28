"""Fresh rechecks see only their evidence package and cannot certify resolution."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest
import test_qc_chat_runtime as fixtures

from vwb.agents import provider_loop
from vwb.qc.chat import QCWorkspace, configuration
from vwb.qc.chat_store import QCStore
from vwb.qc.recheck import INSTRUCTIONS, run
from vwb.tools import build_registry, qc_chat_tools
from vwb.util.hashing import sha256_file, sha256_json
from vwb.util.jsonio import write_json


def newer(parent, version=2):
    data = json.loads((parent.canvas / 'short/v1.json').read_text())
    data['version'] = version
    data['provenance']['sha256'] = sha256_json({k: v for k, v in data.items() if k != 'provenance'})
    write_json(parent.canvas / f'short/v{version}.json', data)
    (parent.canvas / 'short/HEAD').write_text(str(version))
    render = parent.renders / f'short/v{version}/short_v{version}.mp4'
    render.parent.mkdir(parents=True)
    render.write_bytes(f'render-v{version}'.encode())
    write_json(render.parent / 'receipt.json', {'output': 'short', 'version': version, 'file': str(render),
               'sha256': sha256_file(render), 'frames': 48, 'fps': 24,
               'inputs': {'canvas_sha256': data['provenance']['sha256'], 'media': {}}})


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    parent = fixtures.parent.__wrapped__(tmp_path)
    store = QCStore(parent)
    item = store.create({'target': 'output:short@v1', 'f0': 2, 'f1': 8,
                         'expected': 'Footnote fits', 'observed': 'Footnote clipped', 'lane': 'audit'})
    store.create({'target': 'output:short@v1', 'f0': 10, 'f1': 12,
                  'expected': 'UNRELATED_FINDING_SECRET', 'observed': 'Other issue'})
    qws = QCWorkspace(parent)
    qws.initialize()
    qws.add_chat('human', 'QC_CONVERSATION_SECRET')
    newer(parent)
    store.reconcile()
    item = store.get(item['id'])
    target = store.verify_target('output:short@v2')
    cfg = configuration(parent)
    cfg['qc_recheck_instructions'] = INSTRUCTIONS
    record = store.prepare_recheck(item['id'], target['target'], f0=3, f1=9,
                                    expected_revision=item['revision'], idempotency_key='check-1',
                                    pin={k: target[k] for k in ('sha256', 'canvas_file_sha256')},
                                    model=cfg['model'], provider=cfg['provider'])
    def frames(path, selected):
        fill = 0 if 'v1' in str(path) else 200
        return {frame: np.full((48, 64, 3), fill, dtype=np.uint8) for frame in selected}
    monkeypatch.setattr(qc_chat_tools, 'read_frames', frames)
    return parent, store, record, cfg


def tool(ident, name, args):
    return {'type': 'tool_use', 'id': ident, 'name': name, 'input': args}


def inspect_response(*extra):
    return fixtures.reply(blocks=[
        tool('before', 'qc_view', {'target': 'output:short@v1', 'frame': 2}),
        tool('after', 'qc_view', {'target': 'output:short@v2', 'frame': 3}), *extra], reason='tool_use')


def report_response(assessment='not_observed'):
    return fixtures.reply(blocks=[tool('report', 'qc_recheck_report', {
        'assessment': assessment, 'summary': 'The clipping is absent in the sampled newer frame.'})], reason='tool_use')


def test_fresh_context_requires_delivered_evidence_and_preserves_human_state(prepared, monkeypatch):
    parent, store, record, cfg = prepared
    parent_before = parent.request_path.read_bytes()
    qc_before = QCWorkspace(parent).request_path.read_bytes()
    item_before = store.get(record['item_id'])
    responses = iter([
        inspect_response(tool('too-soon', 'qc_recheck_report', {'assessment': 'not_observed', 'summary': 'Premature'})),
        report_response(), fixtures.reply('Review finished.')])
    calls = []
    monkeypatch.setattr(provider_loop, '_post', lambda p, b, activity=None: (calls.append(copy.deepcopy(b)) or next(responses)))
    result = run(parent, record['id'], cfg=cfg)['recheck']
    assert result['status'] == 'completed'
    assert result['result']['assessment'] == 'not_observed'
    assert result['result']['qualification'] == 'unqualified_visual_review'
    assert result['result']['coverage'] == 'sampled_frames_only'
    assert [row['frames'] for row in result['result']['evidence']] == [[2], [3]]
    assert 'receive their image results' in json.dumps(calls[1])
    def images(value):
        if isinstance(value, dict):
            return int(value.get('type') == 'image') + sum(images(v) for v in value.values())
        return sum(images(v) for v in value) if isinstance(value, list) else 0
    assert images(calls[1]['messages']) == 2
    assert {t['name'] for t in calls[0]['tools']} == {'qc_targets', 'qc_view', 'qc_frames', 'qc_recheck_report'}
    for secret in ('EDITOR_REASONING_SECRET', 'EDITOR_BRIEF', 'QC_CONVERSATION_SECRET', 'UNRELATED_FINDING_SECRET'):
        assert secret not in json.dumps(calls)
    assert store.get(record['item_id']) == item_before
    assert parent.request_path.read_bytes() == parent_before
    assert QCWorkspace(parent).request_path.read_bytes() == qc_before
    assert store.list_items(audience='editor') == []
    # A replay after durable completion does not buy another generation.
    assert run(parent, record['id'], cfg=cfg)['recheck']['id'] == record['id']
    assert len(calls) == 2


def test_retry_creates_new_history_and_does_not_reuse_failed_attempt_evidence(prepared, monkeypatch):
    parent, store, record, cfg = prepared
    first = True
    def failed(provider, body, activity=None):
        nonlocal first
        if first:
            first = False
            return inspect_response(tool('bad', 'file_write', {'path': 'owned.txt', 'text': 'x'}))
        raise provider_loop.ProviderError('Synthetic interruption')
    monkeypatch.setattr(provider_loop, '_post', failed)
    with pytest.raises(provider_loop.ProviderError):
        run(parent, record['id'], cfg=cfg)
    assert store.get_recheck(record['id'])['status'] == 'prepared'
    calls = []
    responses = iter([report_response(), fixtures.reply('No evidence available')])
    monkeypatch.setattr(provider_loop, '_post', lambda p, b, activity=None: (calls.append(copy.deepcopy(b)) or next(responses)))
    result = run(parent, record['id'], cfg=cfg)['recheck']
    assert result['result']['assessment'] == 'inconclusive'
    assert result['result']['evidence'] == []
    assert 'Inspect both selected windows' in json.dumps(calls[1])
    assert len(calls[0]['messages']) == 1
    attempts = parent.root / '.qc-chat/rechecks' / record['id']
    assert len(list(attempts.glob('*/memory/providers/*.json'))) == 2
    assert not list(attempts.glob('*/work/owned.txt'))


def test_window_fences_and_forbidden_tools_never_expand_evidence(prepared, monkeypatch):
    parent, store, record, cfg = prepared
    responses = iter([fixtures.reply(blocks=[
        tool('outside', 'qc_view', {'target': 'output:short@v2', 'frame': 9}),
        tool('unrelated', 'qc_items_read', {}),
        tool('write', 'qc_item_create', {'target': 'output:short@v2'}),
    ], reason='tool_use'), fixtures.reply('Could not assess')])
    calls = []
    monkeypatch.setattr(provider_loop, '_post', lambda p, b, activity=None: (calls.append(copy.deepcopy(b)) or next(responses)))
    result = run(parent, record['id'], cfg=cfg)['recheck']
    assert result['result']['assessment'] == 'inconclusive'
    assert result['result']['evidence'] == []
    assert 'inside the selected' in json.dumps(calls[1])
    assert 'may not call qc_items_read' in json.dumps(calls[1])
    assert 'may not call qc_item_create' in json.dumps(calls[1])


def test_human_change_during_review_keeps_result_as_stale_observation(prepared, monkeypatch):
    parent, store, record, cfg = prepared
    responses = iter([inspect_response(), report_response(), fixtures.reply()])
    def post(provider, body, activity=None):
        answer = next(responses)
        if answer['content'][0].get('name') == 'qc_recheck_report':
            store.transition(record['item_id'], 'dismissed', expected_revision=record['item_revision'], note='Human decision')
        return answer
    monkeypatch.setattr(provider_loop, '_post', post)
    result = run(parent, record['id'], cfg=cfg)['recheck']
    assert result['status'] == 'completed' and result['stale']
    assert store.get(record['item_id'])['status'] == 'dismissed'


def test_report_on_last_step_finishes_without_another_generation(prepared, monkeypatch):
    parent, store, record, cfg = prepared
    cfg['max_turns'] = 2
    calls = []
    responses = iter([inspect_response(), report_response()])
    def post(provider, body, activity=None):
        calls.append(body)
        if len(calls) > 2:
            pytest.fail('An accepted report must not require another generation')
        return next(responses)
    monkeypatch.setattr(provider_loop, '_post', post)
    assert run(parent, record['id'], cfg=cfg)['recheck']['status'] == 'completed'
    assert store.get_recheck(record['id'])['result']['assessment'] == 'not_observed'
    assert len(calls) == 2


def test_recheck_truncation_never_raises_output_allowance_above_cap(prepared, monkeypatch):
    parent, store, record, cfg = prepared
    cfg['max_output_tokens'] = 128000
    limits = []
    def post(provider, body, activity=None):
        limits.append(body['max_tokens'])
        return fixtures.reply('Incomplete', reason='max_tokens')
    monkeypatch.setattr(provider_loop, '_post', post)
    with pytest.raises(provider_loop.ProviderError, match='8,192-token'):
        run(parent, record['id'], cfg=cfg)
    assert limits == [8192]
    assert store.get_recheck(record['id'])['status'] == 'prepared'


@pytest.mark.parametrize('change', ['media', 'finding', 'model', 'no-images'])
def test_invalidated_submission_never_calls_provider(prepared, monkeypatch, change):
    parent, store, record, cfg = prepared
    if change == 'media':
        (parent.renders / 'short/v2/short_v2.mp4').write_bytes(b'mutated')
    elif change == 'finding':
        store.transition(record['item_id'], 'resolved', expected_revision=record['item_revision'])
    elif change == 'model':
        cfg['model'] = 'different-model'
    else:
        cfg['image_input_supported'] = False
    monkeypatch.setattr(provider_loop, '_post', lambda *a, **k: pytest.fail('Provider must not be called'))
    with pytest.raises(ValueError):
        run(parent, record['id'], cfg=cfg)


def test_rechecker_report_is_not_available_to_other_roles():
    registry = build_registry()
    for role in ('qc_lead', 'job_lead', 'worker', 'critic', 'scene_miner'):
        assert 'qc_recheck_report' not in {tool.name for tool in registry.for_role(role)}
