"""Portable host configuration without exporting vendor keys or OAuth grants."""
from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest
from test_integrations import FakeHTTP, FakeMCP

from vwb import config
from vwb.integrations import IntegrationError, IntegrationService
from vwb.integrations.callbacks import public_origin

ORIGIN = 'https://workbench.example'
CALLBACK = ORIGIN + '/api/integrations/solstice/callback'


@pytest.fixture
def host_env(tmp_path, monkeypatch):
    for key in config._HOST_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / '.env'
    path.write_text(f'VWB_UI_PUBLIC_URL={ORIGIN}\n'
                    'VWB_SOLSTICE_MCP_URL=https://api.solsticehealth.co/mcp\n'
                    'VWB_SOLSTICE_CLIENT_ID=registered-workbench-client\n'
                    f'VWB_SOLSTICE_REDIRECT_URI={CALLBACK}\n'
                    'OPENAI_API_KEY=private-test-provider-key\n'
                    'UNRELATED_SECRET=private-test-unrelated\n')
    monkeypatch.setenv('VWB_ENV_FILE', str(path))
    return path


def test_host_settings_read_explicit_env_file_without_exporting_it(host_env):
    before = dict(os.environ)
    assert config.host_setting('VWB_SOLSTICE_CLIENT_ID') == 'registered-workbench-client'
    assert config.host_setting('VWB_SOLSTICE_REDIRECT_URI') == CALLBACK
    assert public_origin() == ORIGIN
    assert dict(os.environ) == before
    for name in ['OPENAI_API_KEY', 'UNRELATED_SECRET', 'VWB_RUNS_DIR']:
        with pytest.raises(ValueError, match='Unknown public host setting'):
            config.host_setting(name)


def test_environment_overrides_env_file_including_explicit_disable(host_env, monkeypatch):
    monkeypatch.setenv('VWB_SOLSTICE_CLIENT_ID', 'service-environment-client')
    assert config.host_setting('VWB_SOLSTICE_CLIENT_ID') == 'service-environment-client'
    monkeypatch.setenv('VWB_SOLSTICE_CLIENT_ID', '')
    assert config.host_setting('VWB_SOLSTICE_CLIENT_ID', 'fallback') == ''
    monkeypatch.setenv('VWB_UI_PUBLIC_URL', '')
    assert public_origin() == ''
    assert public_origin('https://flag.example') == 'https://flag.example'


def test_missing_file_defaults_and_nearest_file_discovery(tmp_path, host_env, monkeypatch):
    monkeypatch.setenv('VWB_ENV_FILE', str(tmp_path / 'missing.env'))
    assert config.host_setting('VWB_SOLSTICE_MCP_URL', 'fallback') == 'fallback'
    monkeypatch.delenv('VWB_ENV_FILE')
    checkout = tmp_path / 'nested' / 'checkout'
    checkout.mkdir(parents=True)
    monkeypatch.setattr(config, 'REPO_ROOT', checkout)
    assert config.find_env_file() == host_env
    assert config.host_setting('VWB_SOLSTICE_CLIENT_ID') == 'registered-workbench-client'


def test_public_settings_cannot_interpolate_provider_credentials(host_env):
    with host_env.open('a') as file:
        file.write('VWB_SOLSTICE_CLIENT_ID=${OPENAI_API_KEY}\n')
    assert config.host_setting('VWB_SOLSTICE_CLIENT_ID') == '${OPENAI_API_KEY}'
    service = IntegrationService(host_env.parent / 'runs', http=FakeHTTP(), mcp=FakeMCP())
    with pytest.raises(IntegrationError, match='public client ID'):
        service.begin_auth(CALLBACK)
    assert not service.http.calls and not service.mcp.calls


def test_fresh_host_can_complete_pinned_oauth_from_env_without_ui_configuration(host_env):
    http, mcp = FakeHTTP(), FakeMCP()
    service = IntegrationService(host_env.parent / 'runs', http=http, mcp=mcp)
    status = service.status()
    assert status['items'][0]['available'] and not status['items'][0]['connected']
    assert not http.calls and not mcp.calls
    auth = service.begin_auth(CALLBACK)
    query = parse_qs(urlsplit(auth['authorization_url']).query)
    assert query['client_id'] == ['registered-workbench-client']
    assert query['redirect_uri'] == [CALLBACK]
    assert query['code_challenge_method'] == ['S256']
    service.complete_auth('fixture-code', query['state'][0], CALLBACK)
    assert service.status()['items'][0]['connected']
    assert service.status()['items'][0]['persistent']
    public = json.dumps(service.status())
    assert not any(secret in public for secret in ['private-test-provider-key', 'private-test-unrelated',
                                                  'private-access', 'private-refresh'])
    assert 'client_secret' not in http.calls[0][2]['form']
    with host_env.open('a') as file:
        file.write('VWB_SOLSTICE_CLIENT_ID=different-env-client\n')
    # Successful sign-in pins application identity to its durable OAuth grant.
    assert service.status()['config']['client_id'] == 'registered-workbench-client'


def test_saved_ui_settings_override_env_defaults_and_reject_wrong_origin(host_env):
    service = IntegrationService(host_env.parent / 'runs', http=FakeHTTP(), mcp=FakeMCP())
    service.configure({'client_id': 'saved-ui-client'})
    assert service.status()['config']['client_id'] == 'saved-ui-client'
    with pytest.raises(IntegrationError, match='registered Workbench callback'):
        service.begin_auth('https://unregistered.example/api/integrations/solstice/callback')
    assert not service.http.calls and not service.mcp.calls


@pytest.mark.parametrize('override', [None, 'https://flag.example'])
def test_serve_uses_env_file_origin_and_flag_takes_precedence(host_env, monkeypatch, capsys, override):
    from vwb.web import server
    origin = override or ORIGIN
    before = dict(os.environ)

    class FakeServer:
        def __init__(self, address, app):
            self.app, self.server_port = app, address[1]

        def serve_forever(self, **kwargs):
            assert self.app.public_access.origin == origin
            assert public_origin() == origin
            assert config.host_setting('VWB_SOLSTICE_CLIENT_ID') == 'registered-workbench-client'
            assert os.environ.get('OPENAI_API_KEY') == before.get('OPENAI_API_KEY')

        def server_close(self):
            pass

    monkeypatch.setattr(server, 'WorkbenchServer', FakeServer)
    server.serve(port=8766, open_browser=False, runs_dir=host_env.parent / 'runs', public_url=override)
    assert dict(os.environ) == before
    output = capsys.readouterr().out
    assert origin + '/api/integrations/solstice/callback' in output
    assert 'private-test-provider-key' not in output and 'private-test-unrelated' not in output
