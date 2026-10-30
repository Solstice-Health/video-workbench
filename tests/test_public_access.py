"""Real HTTP boundaries for an explicitly configured, protected public origin."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.cookies import SimpleCookie

import pytest

from vwb.integrations.service import IntegrationService
from vwb.web.server import Workbench, WorkbenchServer

ORIGIN = 'https://workbench.example.ngrok.app'
HOST = 'workbench.example.ngrok.app'


@pytest.fixture
def public_server(tmp_path, monkeypatch):
    monkeypatch.setenv('VWB_UI_PUBLIC_URL', ORIGIN)
    app = Workbench(tmp_path, public_url=ORIGIN)
    server = WorkbenchServer(('127.0.0.1', 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, headers=None):
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={'Host': HOST, **(headers or {})})
        response = conn.getresponse()
        result = response.status, response.headers, response.read()
        conn.close()
        return result
    try:
        yield app, server, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        app.local_imports.close()
        app.studio.close()
        app.queue.close()
        app.close_browsers()


def login(app, request, headers=None):
    status, response, _ = request('POST', '/__workbench/session', {'token': app.public_access.launch_token},
                                  {'Origin': ORIGIN, **(headers or {})})
    assert status == 204
    values = response.get_all('Set-Cookie')
    cookie = SimpleCookie()
    for value in values:
        cookie.load(value)
    return '; '.join(f'{key}={value.value}' for key, value in cookie.items()), values


@pytest.mark.parametrize('path', ['/api/bootstrap', '/api/integrations', '/api/requests', '/static/app.js',
                                 '/api/requests/a/workspace/file?path=work/private.txt'])
def test_anonymous_requests_cannot_read_workbench(public_server, path):
    app, _, request = public_server
    status, headers, body = request('GET', path)
    assert status == 401 and 'csrf_token' not in body.decode()
    assert app.csrf_token not in body.decode() and app.public_access.launch_token not in body.decode()
    assert headers['Cache-Control'] == 'no-store'


def test_launch_page_never_contains_host_grant_and_fragment_is_removed(public_server):
    app, _, request = public_server
    status, headers, body = request('GET', '/', headers={'Sec-Fetch-Site': 'cross-site'})
    assert status == 200
    assert app.public_access.launch_token not in body.decode() and app.public_access.session_token not in body.decode()
    assert 'location.hash' in body.decode() and 'history.replaceState' in body.decode()
    assert headers['Referrer-Policy'] == 'no-referrer' and "frame-ancestors 'none'" in headers['Content-Security-Policy']
    assert '#access=' in app.public_access.launch_url and '?access=' not in app.public_access.launch_url


def test_login_is_origin_bound_and_sets_secure_host_only_sessions(public_server):
    app, _, request = public_server
    assert request('POST', '/__workbench/session', {'token': 'wrong'}, {'Origin': ORIGIN})[0] == 401
    assert request('POST', '/__workbench/session', {'token': app.public_access.launch_token}, {'Origin': 'https://evil.test'})[0] == 403
    assert request('POST', '/__workbench/session', {'token': app.public_access.launch_token}, {'Sec-Fetch-Site': 'cross-site'})[0] == 403
    cookie, values = login(app, request)
    assert all('HttpOnly' in value and 'Secure' in value and 'Domain=' not in value for value in values)
    assert any(value.startswith('__Host-vwb_access=') and 'SameSite=Lax' in value for value in values)
    assert any('vwb_qc_' in value and 'SameSite=Strict' in value for value in values)
    status, _, body = request('GET', '/api/bootstrap', headers={'Cookie': cookie})
    assert status == 200 and json.loads(body)['csrf_token'] == app.csrf_token
    assert request('GET', '/api/bootstrap', headers={'Cookie': cookie, 'Origin': 'https://evil.test'})[0] == 403
    assert request('GET', '/api/bootstrap', headers={'Cookie': cookie, 'Sec-Fetch-Site': 'cross-site'})[0] == 403
    navigation = {'Cookie': cookie, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
    assert request('GET', '/', headers=navigation)[0] == 200
    assert request('GET', '/api/bootstrap', headers=navigation)[0] == 403


def test_forged_proxy_headers_and_host_rewrites_do_not_bypass_auth(public_server):
    _, server, request = public_server
    assert request('GET', '/api/bootstrap', headers={'Host': f'127.0.0.1:{server.server_port}'})[0] == 401
    assert request('GET', '/api/bootstrap', headers={'Host': 'evil.test', 'X-Forwarded-Host': HOST,
        'X-Forwarded-Proto': 'https', 'Forwarded': f'host={HOST};proto=https'})[0] == 403
    conn = HTTPConnection('127.0.0.1', server.server_port)
    conn.putrequest('GET', '/', skip_host=True)
    conn.putheader('Host', HOST)
    conn.putheader('Host', f'127.0.0.1:{server.server_port}')
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 403
    response.read()
    conn.close()


def test_local_launch_is_also_protected_in_public_mode(public_server):
    app, server, request = public_server
    host = f'127.0.0.1:{server.server_port}'
    cookie, values = login(app, request, {'Origin': f'http://{host}', 'Host': host})
    assert all('Secure' not in value for value in values)
    assert 'vwb_local_access_' in cookie and '__Host-' not in cookie
    assert request('GET', '/api/bootstrap', headers={'Host': host, 'Cookie': cookie})[0] == 200
    assert request('GET', '/api/bootstrap', headers={'Cookie': cookie})[0] == 401


def test_mutations_keep_csrf_and_exact_https_oauth_callback(public_server, monkeypatch):
    app, _, request = public_server
    cookie, _ = login(app, request)
    headers = {'Cookie': cookie, 'Origin': ORIGIN}
    path = '/api/integrations/connect'
    assert request('POST', path, {}, headers)[0] == 403
    seen = []
    monkeypatch.setattr(IntegrationService, 'begin_auth', lambda self, redirect_uri: seen.append(redirect_uri) or {'authorization_url': 'https://login.example/authorize'})
    headers['X-CSRF-Token'] = app.csrf_token
    assert request('POST', path, {}, headers)[0] == 200
    assert seen == [ORIGIN + '/api/integrations/solstice/callback']
    completed = []
    monkeypatch.setattr(IntegrationService, 'complete_auth', lambda self, code, state, redirect_uri: completed.append(redirect_uri))
    callback = '/api/integrations/solstice/callback?code=fixture&state=fixture'
    assert request('GET', callback)[0] == 401
    assert request('GET', callback, headers={'Cookie': cookie, 'Sec-Fetch-Site': 'cross-site'})[0] == 200
    assert completed == [ORIGIN + '/api/integrations/solstice/callback']


def test_preview_bypass_still_requires_exact_capability(public_server):
    _, _, request = public_server
    assert request('GET', '/preview/unknown/invalid/work/file.html')[0] == 403
    assert request('GET', '/artifact-preview/unknown/invalid/item/1/index.html')[0] in {403, 404}


def test_old_session_cannot_authorize_new_process(public_server):
    from vwb.web.public_access import PublicAccess
    app, _, request = public_server
    cookie, _ = login(app, request)
    old_grant = app.public_access.launch_token
    app.public_access = PublicAccess(ORIGIN)
    assert request('GET', '/api/bootstrap', headers={'Cookie': cookie})[0] == 401
    assert request('POST', '/__workbench/session', {'token': old_grant})[0] == 401
