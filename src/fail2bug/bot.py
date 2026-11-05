import json
import logging
import re
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .chat import ChatController
from .config import Settings
from .feishu import FeishuClient, FeishuError
from .service import Service
from .store import Store
from .management import Configuration

LOGGER = logging.getLogger(__name__)


class IdleExecutor:
    """配置变更时等待消息处理完成，再原地重启，避免丢消息或打断提单。"""
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.gate = threading.RLock()
        self.pending = 0

    def receive(self, handler, data):
        with self.gate:
            handler(data)

    def submit(self, callback, *args):
        with self.gate:
            self.pending += 1
            def work():
                try:
                    callback(*args)
                finally:
                    with self.gate:
                        self.pending -= 1
            try:
                return self.pool.submit(work)
            except BaseException:
                self.pending -= 1
                raise

    def when_idle(self, callback):
        with self.gate:
            if self.pending == 0:
                callback()

    def shutdown(self):
        self.pool.shutdown(wait=True)


def watch_configuration(configuration, revision, executor, stopped, restart):
    while not stopped.wait(1):
        try:
            if configuration.revision() != revision:
                executor.when_idle(restart)
        except Exception:
            LOGGER.error('配置重新加载未完成，将稍后重试。')


def _post_text(content):
    if "content" not in content:
        content = next((content[key] for key in ("zh_cn", "en_us", "ja_jp") if isinstance(content.get(key), dict)), {})
    paragraphs = content.get("content")
    if not isinstance(paragraphs, list):
        return ""
    lines = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, list):
            continue
        parts = []
        for node in paragraph:
            if not isinstance(node, dict):
                continue
            if node.get("tag") == "text" and isinstance(node.get("text"), str):
                parts.append(node["text"])
            elif node.get("tag") == "a" and isinstance(node.get("href"), str):
                parts.append(" " + node["href"] + " ")
        lines.append("".join(parts))
    return "\n".join(lines).strip()


def make_message_handler(settings, store, client, bot_id, service, executor):
    allowed = {value.strip() for value in settings.allowed_users.split(',') if value.strip()}
    controller = ChatController(settings,store,client,service)

    def identity(chat_id, open_id):
        path = settings.database.parent / 'last-identity.json'
        path.write_text(json.dumps({'open_id':open_id,'at':datetime.now(timezone.utc).isoformat()},ensure_ascii=False))
        LOGGER.info('收到用户主动查询 ID。')
        try:
            access_note = ('你已在允许用户列表中，发送“帮助”可查看使用方法。' if open_id in allowed
                           else '请让管理员将此 ID 加入“管理设置 → 飞书机器人 → 允许用户”。查询 ID 不会自动开通解析权限。')
            client.send_text(chat_id,f'你的飞书 open_id：\n{open_id}\n\n通过机器人发起的批次会自动使用本人作为提交人。\n{access_note}')
        except FeishuError as exc:
            LOGGER.error('ID 已记录，但消息回复失败：%s',exc)

    def process(chat_id,open_id,content,message_type,message_id):
        try:
            if message_type=='text':
                controller.handle_text(chat_id,content.get('text',''),open_id)
            else:
                controller.handle_file(chat_id,open_id,message_id,content,message_type)
        except (ValueError,KeyError) as exc:
            try:
                controller.say(chat_id,'请补充或检查',str(exc) if not isinstance(exc,KeyError) else '没有找到对应批次或文件，请重新选择。')
            except FeishuError:
                LOGGER.error('无法发送处理结果，请检查飞书消息权限。')
        except Exception:
            LOGGER.exception('聊天操作失败')
            try:client.send_text(chat_id,'处理遇到异常，请检查服务日志后重试；未自动确认写入。')
            except FeishuError:pass

    def on_message(data):
        event=data.event;message=event.message;sender=event.sender
        open_id=sender.sender_id.open_id if sender and sender.sender_id else ''
        if not open_id or getattr(sender,'sender_type','user')!='user':return
        if message.message_type not in {'text','post','file','image','media'}:return
        if message.chat_type!='p2p' and not any(mention.id and mention.id.open_id==bot_id for mention in (message.mentions or [])):return
        try:content=json.loads(message.content)
        except (ValueError,TypeError):return
        if not isinstance(content, dict):return
        message_type = message.message_type
        if message_type == 'post':
            content, message_type = {'text': _post_text(content)}, 'text'
        if message_type=='text':
            text=re.sub(r'@_user_\d+','',content.get('text','')).strip();content['text']=text
            is_identity = text.lower() in {'我的id','whoami'}
            if message.message_type == 'post' and not (text.startswith('解析') or is_identity):return
            # 群聊已在入口校验确实 @ 了本机器人；查询本人 ID 不要求先进入允许列表。
            if is_identity:
                if store.reserve_event(data.header.event_id or message.message_id):executor.submit(identity,message.chat_id,open_id)
                return
        if open_id not in allowed:return
        event_id=data.header.event_id or message.message_id
        if not store.reserve_event(event_id):return
        LOGGER.info('收到飞书消息，类型：%s', message.message_type)
        executor.submit(process,message.chat_id,open_id,content,message_type,message.message_id)
    return on_message


def run_bot():
    try:import lark_oapi as lark
    except ImportError as exc:raise SystemExit('请先安装机器人依赖：pip install -e ".[bot]"') from exc
    base=Settings()
    store=Store(base.database)
    configuration=Configuration(base,store)
    revision,settings=configuration.snapshot()
    if settings.mode!='live':raise SystemExit('机器人需要 live 模式及飞书应用配置。')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    executor=IdleExecutor()
    stopped=threading.Event()
    def restart():
        LOGGER.info('管理配置已更新，正在重新连接机器人。')
        os.execv(sys.executable,[sys.executable,'-u','-m','fail2bug','bot'])
    watcher=threading.Thread(target=watch_configuration,args=(configuration,revision,executor,stopped,restart),daemon=True)
    watcher.start()
    client=None
    try:
        if not settings.bot_enabled:
            LOGGER.info('机器人已在本机管理页停用；重新启用后会自动连接。')
            stopped.wait()
            return
        client=FeishuClient(settings);bot_id=client.bot_open_id();service=Service(settings,store,client)
        on_message=make_message_handler(settings,store,client,bot_id,service,executor)
        handler=lark.EventDispatcherHandler.builder('','').register_p2_im_message_receive_v1(lambda data:executor.receive(on_message,data)).build()
        ws=lark.ws.Client(settings.app_id,settings.app_secret.get_secret_value(),event_handler=handler,log_level=lark.LogLevel.WARNING)
        LOGGER.info('机器人启动中，支持执行表及 xlsx/zip 导入，返回网页审核链接。')
        ws.start()
    except KeyboardInterrupt:pass
    finally:
        stopped.set()
        executor.shutdown()
        if client:client.close()
