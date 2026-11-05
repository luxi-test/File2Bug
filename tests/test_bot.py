import json
from types import SimpleNamespace as NS

import pytest

from fail2bug.bot import make_message_handler
from fail2bug.config import Settings
from fail2bug.store import Store


class ImmediateExecutor:
    def submit(self, callback, *args):
        callback(*args)


class Client:
    def __init__(self):
        self.messages = []

    def send_text(self, chat_id, text):
        self.messages.append((chat_id, text))


class Service:
    def __init__(self,store):
        self.calls = []
        self.store=store

    def import_sheet(self, url, reporter):
        self.calls.append((url, reporter))
        return self.store.create({'items':[],'counts':{'unknown':0}},target='demo',mode='demo',title='测试',reporter=reporter)

    def review(self,batch_id):
        return self.store.get(batch_id)


def event(text, event_id="e1", chat_type="p2p"):
    return NS(header=NS(event_id=event_id), event=NS(
        message=NS(message_type="text", chat_type=chat_type, mentions=[], content=json.dumps({"text": text}), chat_id="chat", message_id="msg"),
        sender=NS(sender_type="user", sender_id=NS(open_id="ou_me")),
    ))


def test_identity_works_before_allowlist_without_granting_access(tmp_path):
    settings = Settings(_env_file=None, database=tmp_path / "db.sqlite")
    store=Store(settings.database)
    client, service = Client(), Service(store)
    handler = make_message_handler(settings, store, client, "ou_bot", service, ImmediateExecutor())
    handler(event("我的ID"))
    handler(event("我的ID"))
    assert len(client.messages) == 1
    assert "ou_me" in client.messages[0][1]
    assert json.loads((tmp_path / "last-identity.json").read_text())["open_id"] == "ou_me"
    handler(event("解析 https://test.feishu.cn/sheets/abc", "e2"))
    assert not service.calls


@pytest.mark.parametrize('message_type', ['text', 'post'])
@pytest.mark.parametrize('command', ['我的ID', 'whoami'])
def test_group_identity_uses_sender_before_allowlist_and_deduplicates(tmp_path, message_type, command):
    settings = Settings(_env_file=None, database=tmp_path/'db.sqlite')
    store = Store(settings.database)
    client, service = Client(), Service(store)
    handler = make_message_handler(settings, store, client, 'ou_bot', service, ImmediateExecutor())
    incoming = event('@_user_1 '+command, chat_type='group')
    incoming.event.sender.sender_id.open_id = 'ou_colleague'
    incoming.event.message.mentions = [NS(id=NS(open_id='ou_bot'))]
    if message_type == 'post':
        incoming.event.message.message_type = 'post'
        incoming.event.message.content = json.dumps({'zh_cn': {'content': [[
            {'tag': 'at', 'user_id': 'ou_bot'}, {'tag': 'text', 'text': ' '+command},
        ]]}})
    handler(incoming)
    handler(incoming)
    assert len(client.messages) == 1
    assert 'ou_colleague' in client.messages[0][1]
    assert 'ou_bot' not in client.messages[0][1]
    assert '允许用户' in client.messages[0][1]
    assert not service.calls
    assert not store.chat_session('chat', 'ou_colleague')
    # 查到本人 ID 不会自动放开解析权限。
    incoming.header.event_id = 'parse-after-query'
    incoming.event.message.message_type = 'text'
    incoming.event.message.content = json.dumps({'text': '@_user_1 解析 https://test.feishu.cn/sheets/abc'})
    handler(incoming)
    assert not service.calls


@pytest.mark.parametrize('mentioned', [None, 'ou_other'])
def test_group_identity_requires_mentioning_this_bot(tmp_path, mentioned):
    settings = Settings(_env_file=None, database=tmp_path/'db.sqlite')
    store = Store(settings.database)
    client, service = Client(), Service(store)
    handler = make_message_handler(settings, store, client, 'ou_bot', service, ImmediateExecutor())
    incoming = event('我的ID', chat_type='group')
    incoming.event.message.mentions = [NS(id=NS(open_id=mentioned))] if mentioned else []
    handler(incoming)
    assert not client.messages
    assert not (tmp_path/'last-identity.json').exists()


@pytest.mark.parametrize('link', ['{url}', '[执行结果 V1.0]({url})'])
def test_bot_import_uses_sender_as_reporter(tmp_path, link):
    settings = Settings(_env_file=None, database=tmp_path / "db.sqlite", allowed_users="ou_me")
    store=Store(settings.database)
    client, service = Client(), Service(store)
    handler = make_message_handler(settings, store, client, "ou_bot", service, ImmediateExecutor())
    url = 'https://test.feishu.cn/sheets/abc?sheet=tab1'
    handler(event('解析 ' + link.format(url=url)))
    assert service.calls == [(url, 'ou_me')]


def test_bot_reads_spreadsheet_link_in_rich_text(tmp_path):
    settings = Settings(_env_file=None, database=tmp_path / "db.sqlite", allowed_users="ou_me")
    store = Store(settings.database)
    client, service = Client(), Service(store)
    handler = make_message_handler(settings, store, client, "ou_bot", service, ImmediateExecutor())
    url = "https://test.feishu.cn/sheets/abc?sheet=tab1"
    incoming = event("")
    incoming.event.message.message_type = "post"
    incoming.event.message.content = json.dumps({"content": [[
        {"tag": "text", "text": "解析"},
        {"tag": "a", "text": "测试执行表", "href": url},
    ]]})
    handler(incoming)
    assert service.calls == [(url, "ou_me")]


def test_group_at_bot_uses_actual_colleague_and_keeps_sessions_separate(tmp_path):
    settings = Settings(_env_file=None, database=tmp_path/'db.sqlite', allowed_users='ou_me,ou_colleague')
    store = Store(settings.database)
    client, service = Client(), Service(store)
    handler = make_message_handler(settings, store, client, 'ou_bot', service, ImmediateExecutor())
    for index, user_id in enumerate(('ou_me', 'ou_colleague')):
        incoming = event('@_user_1 解析 https://test.feishu.cn/sheets/abc', f'group-{index}', chat_type='group')
        incoming.event.sender.sender_id.open_id = user_id
        incoming.event.message.mentions = [NS(id=NS(open_id='ou_bot'))]
        handler(incoming)
    assert [reporter for _, reporter in service.calls] == ['ou_me', 'ou_colleague']
    first = store.chat_session('chat', 'ou_me')['batch_id']
    second = store.chat_session('chat', 'ou_colleague')['batch_id']
    assert first != second
    assert store.get(second)['defaults']['reporter'] == 'ou_colleague'
