"""Real loopback HTTP exercises studio CSRF, upload and async job contracts."""
from __future__ import annotations

import io
import json
import sys
import threading
import time
from http.client import HTTPConnection

import pytest
from PIL import Image

from vwb.studio.service import StudioJobs
from vwb.web.queue import TurnQueue
from vwb.web.server import Workbench, WorkbenchServer
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def http(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / 'r-studio')
    for part in SUBDIRS:
        (ws.root / part).mkdir(parents=True)
    ws.save(Request(id=ws.id, message='Studio fixture'))
    worker = tmp_path / 'stub.py'
    worker.write_text("import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.with_suffix('.result').write_text(json.dumps({'ok':True,'result':{'artifacts':[]}}))")
    monkeypatch.setattr('vwb.studio.service.capabilities', lambda: {'image': {'available': True}})
    app = Workbench(tmp_path, queue=TurnQueue(tmp_path))
    app.studio.close()
    app.studio = StudioJobs(command=[sys.executable, str(worker)])
    server = WorkbenchServer(('127.0.0.1', 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, endpoint, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
        supplied = {'X-CSRF-Token': app.csrf_token, **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body)
            supplied['Content-Type'] = 'application/json'
        connection.request(method, '/api/requests/r-studio/studio/' + endpoint, body, supplied)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data
    try:
        yield request, app, ws
    finally:
        app.studio.close()
        app.queue.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_studio_http_upload_csrf_idempotent_jobs_and_reusable_inputs(http):
    request, app, ws = http
    assert request('GET', 'capabilities')[0] == 200
    body = io.BytesIO()
    Image.new('RGBA', (64, 64), 'white').save(body, format='PNG')
    headers = {'X-Filename': 'source.png', 'X-Upload-Kind': 'image', 'Content-Type': 'image/png'}
    assert request('POST', 'uploads', body.getvalue(), {**headers, 'X-CSRF-Token': ''})[0] == 403
    assert request('POST', 'uploads', body.getvalue(), {**headers, 'Origin': 'https://evil.example'})[0] == 403
    status, uploaded = request('POST', 'uploads', body.getvalue(), headers)
    assert status == 201 and uploaded['url'].startswith('/preview/r-studio/')
    job_input = {'kind': 'image', 'operation': 'edit', 'prompt': 'Make it blue',
                 'idempotency_key': 'http-submission-0001', 'inputs': [{'upload_id': uploaded['upload_id']}]}
    status, first = request('POST', 'jobs', job_input)
    assert status == 202
    assert 'request_root' not in first and 'prepared' not in first
    assert first['reusable_inputs'][0]['url'].startswith('/preview/r-studio/')
    assert first['reusable_inputs'][0]['ref'].startswith('slot:')
    assert request('POST', 'jobs', job_input)[1]['id'] == first['id']
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        row = request('GET', 'jobs/' + first['id'])[1]
        if row['status'] == 'completed':
            break
        time.sleep(.03)
    assert row['status'] == 'completed'
    assert len(request('GET', 'jobs?kind=image')[1]['jobs']) == 1
    assert request('GET', 'jobs?kind=invalid')[0] == 400
    assert request('POST', 'jobs/' + first['id'] + '/cancel', {})[1]['status'] == 'completed'
    assert request('POST', 'jobs', {**job_input, 'model': 'unsupported'})[0] == 400


def test_studio_http_empty_mask_and_path_escape_are_rejected(http):
    request, _, _ = http
    body = io.BytesIO()
    Image.new('RGBA', (64, 64), (0, 0, 0, 0)).save(body, format='PNG')
    assert request('POST', 'uploads', body.getvalue(), {'X-Filename': 'mask.png', 'X-Upload-Kind': 'mask'})[0] == 400
    result = request('POST', 'jobs', {'kind': 'image', 'operation': 'edit', 'prompt': 'Make it blue',
                                    'idempotency_key': 'http-submission-0001', 'inputs': [{'path': '../../secret.png'}]})
    assert result[0] == 400
    assert request('GET', 'jobs')[1]['jobs'] == []
