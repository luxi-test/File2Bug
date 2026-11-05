import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from fail2bug.app import create_app
from fail2bug.config import Settings
from fail2bug.feishu import FeishuError
from fail2bug.management import Configuration, ConfigUpdate, Keychain, parse_target_url
from fail2bug.parser import InputError
from fail2bug.store import Conflict, Store


class MemoryKeychain:
    def __init__(self):
        self.values = {}

    def get(self, reference):
        return self.values[reference]

    def put(self, value):
        reference = uuid4().hex
        self.values[reference] = value
        return reference

    def delete(self, reference):
        del self.values[reference]


class ReadOnlyClient:
    calls = []
    failure = False

    def __init__(self, settings):
        self.settings = settings
        self.closed = False

    def verify_fields(self):
        self.calls.append('fields')
        if self.failure:
            raise FeishuError('应用没有访问这张表所需的文档权限。')
        return {'标题': {}}

    def bot_open_id(self):
        self.calls.append('bot')
        return 'ou_bot'

    def close(self):
        self.closed = True


@pytest.fixture
def config(tmp_path):
    base = Settings(_env_file=None, mode='live', database=tmp_path/'db.sqlite', app_id='cli_test',
                    app_secret='fixture-old-secret', access_key='fixture-access-key', allowed_users='ou_me',
                    target_app_token='AppOld', target_table_id='tblOld',
                    target_url='https://team.feishu.cn/base/AppOld?table=tblOld')
    ReadOnlyClient.calls = []
    ReadOnlyClient.failure = False
    return Configuration(base, Store(base.database), keychain=MemoryKeychain(), client_factory=ReadOnlyClient)


def payload(config):
    return {key: value for key, value in config.public().items() if key in {'revision', 'values', 'fields'}}


def local_client(config):
    return TestClient(create_app(config.base, configuration=config), base_url='http://127.0.0.1', client=('127.0.0.1', 12345))


def login(client):
    assert client.post('/api/login', json={'access_key':'fixture-access-key'}).status_code == 200


def test_admin_requires_real_loopback_host_full_login_and_same_origin(config):
    app = create_app(config.base, configuration=config)
    cases = [
        ('http://127.0.0.1', ('192.0.2.20', 10), {}),
        ('http://192.0.2.10', ('127.0.0.1', 10), {}),
        ('http://evil.test', ('127.0.0.1', 10), {}),
        ('http://localhost', ('127.0.0.1', 10), {'X-Forwarded-For':'127.0.0.1'}),
    ]
    for base_url, peer, headers in cases:
        with TestClient(app, base_url=base_url, client=peer, headers=headers) as client:
            login(client)
            for path in ('/admin', '/api/admin/config'):
                response = client.get(path)
                assert response.status_code == 403
                assert 'AppOld' not in response.text
            assert client.put('/api/admin/config', json=payload(config)).status_code == 403
            assert '管理设置' not in client.get('/').text
    with local_client(config) as client:
        assert '访问口令' in client.get('/admin').text
        assert client.get('/api/admin/config').status_code == 401
        from fail2bug.access import issue_review_token
        client.cookies.set('f2b_review', issue_review_token(config.base, 'batch'))
        assert client.get('/api/admin/config').status_code == 401
        login(client)
        response = client.get('/admin')
        assert response.status_code == 200 and '管理设置' in response.text
        assert "connect-src 'self'" in response.headers['content-security-policy']
        response = client.put('/api/admin/config', json=payload(config), headers={'Origin':'https://evil.test'})
        assert response.status_code == 403


def test_admin_never_returns_saved_secrets_in_html_json_or_validation_errors(config):
    with local_client(config) as client:
        login(client)
        for path in ('/admin', '/api/admin/config'):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            assert 'fixture-old-secret' not in response.text
            assert 'fixture-access-key' not in response.text
        public = client.get('/api/admin/config').json()
        assert public['secrets']['app_secret_configured'] is True
        assert 'app_secret' not in public['values'] and 'access_key' not in public['values']
        body = payload(config)
        body.update(app_secret='fixture-new-secret', unexpected='not-allowed')
        response = client.put('/api/admin/config', json=body)
        assert response.status_code == 422
        assert 'fixture-new-secret' not in response.text
        assert not config.keychain.values


def test_save_is_persistent_applies_to_next_requests_and_pins_old_batches(config):
    with local_client(config) as client:
        login(client)
        old_service = client.app.state.service
        body = payload(config)
        body['values']['target_url'] = 'https://team.feishu.cn/base/AppNew?table=tblNew&view=vew1'
        body['values']['allowed_users'] = 'ou_me\nou_colleague,ou_me'
        response = client.put('/api/admin/config', json=body)
        assert response.status_code == 200
        assert response.json()['revision'] == 1
        assert response.json()['values']['allowed_users'] == 'ou_me,ou_colleague'
        assert response.json()['values']['target_url'].endswith('AppNew?table=tblNew')
        assert client.app.state.service.settings.target_id == 'AppNew/tblNew'
        assert old_service.settings.target_id == 'AppOld/tblOld'
        from fail2bug.parser import demo_table, parse_table
        old_batch = config.store.create(parse_table(demo_table(), ''), target=old_service.settings.target_id,
                                        mode='live', title='旧批次')
        with pytest.raises(Conflict, match='配置与创建批次时不同'):
            client.app.state.service.submit(old_batch['id'], old_batch['revision'])
        assert ReadOnlyClient.calls == ['fields', 'bot']
        assert client.put('/api/admin/config', json=body).status_code == 409
    reloaded = Configuration(config.base, config.store, keychain=config.keychain)
    assert reloaded.snapshot()[1].target_id == 'AppNew/tblNew'
    assert reloaded.public()['revision'] == 1


def test_new_secret_only_goes_to_keychain_and_blank_preserves_it(config):
    body = payload(config)
    body['app_secret'] = 'fixture-new-secret'
    result = config.save(ConfigUpdate(**body))
    assert result['secrets']['app_secret_configured']
    assert 'fixture-new-secret' not in json.dumps(result)
    with config.store.connection() as conn:
        stored = conn.execute('SELECT body FROM app_settings').fetchone()[0]
    assert 'fixture-new-secret' not in stored and 'fixture-old-secret' not in stored
    assert list(config.keychain.values.values()) == ['fixture-new-secret']
    another = Configuration(config.base, config.store, keychain=config.keychain, client_factory=ReadOnlyClient)
    assert another.snapshot()[1].app_secret.get_secret_value() == 'fixture-new-secret'
    next_body = payload(another)
    next_body['app_secret'] = ''
    another.save(ConfigUpdate(**next_body))
    assert len(config.keychain.values) == 1
    assert config.snapshot()[1].app_secret.get_secret_value() == 'fixture-new-secret'


def test_config_changes_keep_inflight_client_until_request_completes(config):
    from fail2bug.management import Services
    services = Services(config)
    with services.acquire() as original:
        body = payload(config)
        body['values']['default_project'] = '新项目'
        config.save(ConfigUpdate(**body))
        current = services.current()
        assert current is not original
        assert current.settings.default_project == '新项目'
        assert not original.client.closed
        assert len(services._clients) == 2
    assert original.client.closed
    assert len(services._clients) == 1
    services.close()


def test_bad_configuration_never_replaces_working_settings(config):
    body = payload(config)
    body['values']['app_id'] = 'cli_other'
    with pytest.raises(InputError, match='同时填写'):
        config.save(ConfigUpdate(**body))
    body['app_secret'] = 'fixture-new-secret'
    ReadOnlyClient.failure = True
    with pytest.raises(FeishuError):
        config.save(ConfigUpdate(**body))
    assert config.revision() == 0
    assert not config.keychain.values
    assert config.snapshot()[1].app_id == 'cli_test'


@pytest.mark.parametrize('url', [
    'http://team.feishu.cn/base/App?table=tbl1',
    'https://team.feishu.cn.evil.test/base/App?table=tbl1',
    'https://user:password@team.feishu.cn/base/App?table=tbl1',
    'https://team.feishu.cn/base/App',
    'https://team.feishu.cn/sheets/App?table=tbl1',
    'https://team.feishu.cn:8443/base/App?table=tbl1',
])
def test_target_parser_rejects_unsafe_or_incomplete_urls(url):
    with pytest.raises(InputError):
        parse_target_url(url)


def test_keychain_secret_is_passed_via_stdin_never_process_arguments(monkeypatch):
    from types import SimpleNamespace
    import fail2bug.credentials as module
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'fixture-sensitive-secret\n')
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(module.subprocess, 'run', run)
    Keychain().put('fixture-sensitive-secret')
    assert calls[0][0] == ['/usr/bin/security', '-i']
    assert b'66697874757265' in calls[0][1]['input']
    assert all('fixture-sensitive-secret' not in repr(args) for args, kwargs in calls)


def test_settings_can_load_credentials_from_keychain_references(monkeypatch):
    import fail2bug.credentials as module
    monkeypatch.setattr(module.Keychain, 'get', lambda self, reference: self.service + ':' + reference)
    settings = Settings(_env_file=None, mode='live', app_id='cli_test', target_app_token='app', target_table_id='tbl',
                        app_secret_keychain='app-reference', access_key_keychain='access-reference')
    assert settings.app_secret.get_secret_value() == 'file2bug.app-secret:app-reference'
    assert settings.access_key.get_secret_value() == 'file2bug.access-key:access-reference'


def test_bot_reload_waits_until_processing_finishes():
    from fail2bug.bot import IdleExecutor
    executor = IdleExecutor()
    started, release = threading.Event(), threading.Event()
    restarts = []
    def work():
        started.set()
        assert release.wait(2)
    future = executor.submit(work)
    assert started.wait(2)
    executor.when_idle(lambda: restarts.append(True))
    assert not restarts
    release.set()
    future.result(timeout=2)
    executor.when_idle(lambda: restarts.append(True))
    assert restarts == [True]
    executor.shutdown()
