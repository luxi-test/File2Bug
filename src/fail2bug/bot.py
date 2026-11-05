import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .chat import ChatController
from .config import Settings
from .feishu import FeishuClient, FeishuError
from .service import Service
from .store import Store

LOGGER = logging.getLogger(__name__)


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
        LOGGER.info('收到用户主动查询 ID：%s',open_id)
        try:
            client.send_text(chat_id,f'你的飞书 open_id：\n{open_id}\n\n通过机器人发起的批次会自动使用本人作为提交人。发送“帮助”可查看导入和网页审核方法。')
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
            text = _post_text(content)
            if not text.startswith('解析'):
                return
            content, message_type = {'text': text}, 'text'
        if message_type=='text':
            text=re.sub(r'@_user_\d+','',content.get('text','')).strip();content['text']=text
            if message.chat_type=='p2p' and text.lower() in {'我的id','whoami'}:
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
    settings=Settings()
    if settings.mode!='live':raise SystemExit('机器人需要 live 模式及飞书应用配置。')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    store=Store(settings.database);client=FeishuClient(settings);bot_id=client.bot_open_id();service=Service(settings,store,client)
    executor=ThreadPoolExecutor(max_workers=1)
    on_message=make_message_handler(settings,store,client,bot_id,service,executor)
    handler=lark.EventDispatcherHandler.builder('','').register_p2_im_message_receive_v1(on_message).build()
    ws=lark.ws.Client(settings.app_id,settings.app_secret.get_secret_value(),event_handler=handler,log_level=lark.LogLevel.WARNING)
    try:
        LOGGER.info('机器人启动中，支持执行表及 xlsx/zip 导入，返回网页审核链接。')
        ws.start()
    except KeyboardInterrupt:pass
    finally:executor.shutdown(wait=True);client.close()
