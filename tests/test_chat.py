from pathlib import Path
import shutil

import pytest

from fail2bug.chat import ChatController, assignments
from fail2bug.config import Settings
from fail2bug.inference import FREQUENCIES
from fail2bug.parser import InputError
from fail2bug.service import Service
from fail2bug.store import Store


class Client:
    def __init__(self):
        self.messages=[];self.created=[];self.updated=[];self.file=None
    def verify_fields(self):
        return {
            '问题类型':{'type':4,'property':{'options':[{'name':'功能异常'},{'name':'显示异常'}]}},
            '模块':{'type':4,'property':{'options':[{'name':'相册'}]}},
            '子模块':{'type':3,'property':{'options':[{'name':'相册'},{'name':'查看照片'}]}},
            '发生概率':{'type':3,'property':{'options':[{'name':value} for value in FREQUENCIES.values()]}},
        }
    def read_sheet(self,url):
        return [
            ['用例ID','PRD模块','标题','优先级','操作步骤','预期结果','执行结果','备注'],
            ['CHAT-1','相册','照片保存失败','P2','点击拍照','保存图片','fail','图片没有保存'],
        ],'聊天测试',url
    def read_sheet_media(self, url, row_numbers, column_count, row_count): return []
    def read_float_images(self, url): return []
    def send_text(self,chat_id,text):self.messages.append((chat_id,text))
    def send_card(self,chat_id,title,content):self.messages.append((chat_id,title+'\n'+content))
    def create_record(self,fields,operation_id):
        self.created.append((fields,operation_id));return {'record_id':'rec_mock','record_url':'https://example.invalid/record'}
    def update_record(self,record_id,fields,operation_id):
        self.updated.append((record_id,fields,operation_id));return {'record_id':record_id}
    def download_message_file(self,message_id,key,path,resource_type):shutil.copyfile(self.file,path)


@pytest.fixture
def chat(tmp_path):
    settings=Settings(_env_file=None,mode='live',database=tmp_path/'state.db',app_id='test',app_secret='secret',target_app_token='app',target_table_id='table',access_key='password',field_problem_type='问题类型',field_module='模块',field_submodule='子模块',field_frequency='发生概率')
    store=Store(settings.database);client=Client();service=Service(settings,store,client)
    return ChatController(settings,store,client,service),client


def start(controller):controller.handle_text('chat','解析 https://test.feishu.cn/sheets/abc','ou_me')


def test_chat_can_review_fill_and_submit_without_web(chat):
    controller,client=chat;start(controller)
    batch=controller.batch('chat','ou_me')
    assert batch['items'][0]['problem_type']=='功能异常'
    assert batch['items'][0]['submodule']=='相册'
    assert batch['items'][0]['frequency']==FREQUENCIES['very_high']
    controller.handle_text('chat','详情 1','ou_me')
    assert '【前提】：' in client.messages[-1][1]
    controller.handle_text('chat','设置 版本=1.0 设备=Android 环境=Android','ou_me')
    controller.handle_text('chat','修改 1 发生概率=偶现','ou_me')
    controller.handle_text('chat','确认提交','ou_me')
    assert len(client.created)==1
    assert client.created[0][0]['发生概率']==FREQUENCIES['medium']
    assert '【前提】：' in client.created[0][0]['复现步骤']
    assert '【操作】：' in client.created[0][0]['复现步骤']
    with pytest.raises(InputError,match='已成功'):controller.handle_text('chat','确认提交','ou_me')
    assert len(client.created)==1


def test_chat_rejects_classification_batch_defaults(chat):
    controller,_=chat;start(controller)
    with pytest.raises(InputError,match='没有批次默认'):controller.handle_text('chat','设置 发生概率=必现','ou_me')
    assert 'frequency' not in controller.batch('chat','ou_me')['defaults']


def test_chat_resume_checks_owner(chat):
    controller,_=chat;start(controller)
    batch=controller.batch('chat','ou_me')
    controller.handle_text('other-chat','使用 '+batch['id'][:8],'ou_me')
    assert controller.batch('other-chat','ou_me')['id']==batch['id']
    with pytest.raises(InputError):controller.handle_text('other-chat','使用 '+batch['id'][:8],'ou_other')


def test_chat_file_import_uses_sender_identity(chat,tmp_path):
    from openpyxl import Workbook
    controller,client=chat
    path=tmp_path/'result.xlsx';book=Workbook();page=book.active
    for row in client.read_sheet('')[0]:page.append(row)
    book.save(path);book.close();client.file=path
    controller.handle_file('chat','ou_me','msg',{'file_key':'file','file_name':'result.xlsx'},'file')
    batch=controller.batch('chat','ou_me')
    assert batch['defaults']['reporter']=='ou_me'
    assert len(batch['items'])==1
    assert batch['source_label'].startswith('local:')
    assert f"/review/{batch['id']}#access=" in client.messages[-1][1]
    assert not client.created


def test_import_and_resume_reply_with_current_batch_review_link(chat):
    controller,client=chat
    controller.settings.base_url='http://192.168.1.99:8000/'
    start(controller)
    batch=controller.batch('chat','ou_me')
    for command in ('审核链接', '使用 '+batch['id'][:8]):
        controller.handle_text('chat',command,'ou_me')
        message=client.messages[-1][1]
        assert f"[打开审核与补充页面](http://192.168.1.99:8000/review/{batch['id']}#access=" in message
        assert '24 小时' in message and '同一局域网' in message
        assert '设置 版本=' not in message and not client.created
        assert controller.settings.access_key.get_secret_value() not in message


def test_actual_edit_reinfers_missing_frequency(chat):
    controller,_=chat;start(controller)
    controller.handle_text('chat','修改 1 实际结果=图片没有保存，每次都会发生','ou_me')
    assert controller.batch('chat','ou_me')['items'][0]['frequency']==FREQUENCIES['very_high']


def test_assignment_parser_preserves_spaces_and_multiline():
    result=assignments('实际结果=点击没有反应\n重试仍无反应 发生概率=High / Likely')
    assert result['actual']=='点击没有反应\n重试仍无反应'
    assert result['frequency']=='High / Likely'


def test_failed_new_import_cannot_confirm_previous_batch(chat):
    controller,client=chat;start(controller)
    def fail(url):raise InputError('读取失败')
    client.read_sheet=fail
    with pytest.raises(InputError):controller.handle_text('chat','解析 https://test.feishu.cn/sheets/bad','ou_me')
    with pytest.raises(InputError,match='上次导入未完成'):controller.handle_text('chat','确认提交','ou_me')
    assert not client.created
