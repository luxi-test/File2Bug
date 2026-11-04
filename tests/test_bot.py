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


@pytest.mark.parametrize('link', ['{url}', '[执行结果 V1.0]({url})'])
def test_bot_import_uses_sender_as_reporter(tmp_path, link):
    settings = Settings(_env_file=None, database=tmp_path / "db.sqlite", allowed_users="ou_me")
    store=Store(settings.database)
    client, service = Client(), Service(store)
    handler = make_message_handler(settings, store, client, "ou_bot", service, ImmediateExecutor())
    url = 'https://test.feishu.cn/sheets/abc?sheet=tab1'
    handler(event('解析 ' + link.format(url=url)))
    assert service.calls == [(url, 'ou_me')]
