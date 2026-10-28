"""Explicit recheck submission, durable retries, and private read boundaries."""
import copy
import json

import pytest
import test_qc_chat_http as fixtures
from test_qc_recheck_runtime import newer

from vwb.qc.chat_store import QCStore

parent = fixtures.parent
http = fixtures.http


def selection(http):
    request, app, ws, qws = http
    base = f'/api/requests/{ws.id}/qc'
    item = request('POST', base + '/items', fixtures.finding(ws, lane='audit'))[1]['item']
    newer(ws)
    snapshot = request('GET', base)[1]
    item = next(row for row in snapshot['items'] if row['id'] == item['id'])
    target = next(row for row in snapshot['targets'] if row['version'] == 2)
    return base, {'item_id': item['id'], 'target': target['target'], 'f0': 2, 'f1': 4,
                  'expected_revision': item['revision'], 'idempotency_key': 'recheck-one',
                  'pin': {key: target[key] for key in ('sha256', 'canvas_file_sha256')}}


def test_recheck_is_explicit_queued_private_and_idempotent(http):
    request, app, ws, qws = http
    base, body = selection(http)
    assert request('GET', base)[1]['rechecks'] == []
    assert app.queue.snapshot(qws)['queue'] == []
    assert request('POST', base + '/rechecks', body, auth=False)[0] == 403
    status, first = request('POST', base + '/rechecks', body)
    assert status == 200, first
    assert first['job']['status'] == 'queued'
    assert first['job']['main_config']['qc_recheck_id'] == first['recheck']['id']
    assert len(first['job']['main_config']['qc_targets']) == 2
    assert request('POST', base + '/rechecks', body) == (200, first)
    assert len(app.queue.snapshot(qws)['queue']) == 1
    assert not app.queue.snapshot(ws)['queue']
    listing = request('GET', base)[1]['rechecks']
    assert listing[0]['job']['id'] == first['job']['id']
    public = json.dumps(request('GET', f'/api/requests/{ws.id}')[1])
    assert first['recheck']['id'] not in public
    assert body['item_id'] not in public
    assert request('POST', base + '/rechecks', {**body, 'f1': 5})[0] == 400


@pytest.mark.parametrize('patch', [
    {'f0': -1}, {'f1': 999}, {'f0': True}, {'target': 'output:short@v1'},
    {'expected_revision': 1}, {'pin': {'sha256': '0' * 64, 'canvas_file_sha256': '0' * 64}},
    {'actor': 'human'}, {'idempotency_key': ''}, {'pin': None},
])
def test_recheck_rejects_invalid_or_stale_selection_without_dispatch(http, patch):
    request, app, ws, qws = http
    base, body = selection(http)
    status, response = request('POST', base + '/rechecks', {**body, **patch})
    assert status == 400, response
    assert app.queue.snapshot(qws)['queue'] == []
    assert QCStore(ws).list_rechecks() == []


def test_uncertain_dispatch_retry_reuses_record_configuration_and_job(http, monkeypatch):
    request, app, ws, qws = http
    base, body = selection(http)
    enqueue = app.queue.enqueue
    sent = []
    def interrupted(*args, **kwargs):
        result = enqueue(*args, **kwargs)
        sent.append(copy.deepcopy(result))
        if len(sent) == 1:
            raise ValueError('Synthetic lost response after enqueue')
        return result
    monkeypatch.setattr(app.queue, 'enqueue', interrupted)
    assert request('POST', base + '/rechecks', body)[0] == 400
    # A configuration edit between requests cannot alter this frozen operation.
    monkeypatch.setattr('vwb.web.qc.slot', lambda name: pytest.fail('Must use frozen config on retry'))
    status, result = request('POST', base + '/rechecks', body)
    assert status == 200, result
    assert result['job'] == sent[0] == sent[1]
    assert len(QCStore(ws).list_rechecks()) == 1
    assert len(app.queue.snapshot(qws)['queue']) == 1


def test_model_family_conflict_fails_before_dispatch(http, monkeypatch):
    request, app, ws, qws = http
    base, body = selection(http)
    monkeypatch.setattr('vwb.web.qc.slot', lambda name: {'provider': 'openrouter', 'model': 'openai/gpt-6-astra',
                                                     'image_input_supported': True})
    status, response = request('POST', base + '/rechecks', body)
    assert status == 400 and 'different QC model family' in response['error']
    assert app.queue.snapshot(qws)['queue'] == []
    assert QCStore(ws).list_rechecks() == []
