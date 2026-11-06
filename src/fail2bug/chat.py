"""飞书负责导入和通知，网页为主要审核与补充入口。"""
import logging
import mimetypes
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from uuid import uuid4

from .inference import infer_frequency
from .access import review_url
from .models import ReviewUpdate
from .parser import InputError
from .repro import format_repro

LOGGER = logging.getLogger(__name__)
LABELS = {'优先级':'priority','版本':'version','测试版本':'version','设备':'device','测试设备':'device','环境':'environment','测试环境':'environment','项目':'project','所属项目':'project',
          '问题类型':'problem_type','类型':'problem_type','模块':'module','子模块':'submodule','发生概率':'frequency','发送概率':'frequency','概率':'frequency',
          '标题':'title','缺陷标题':'title','前提':'preconditions','前置条件':'preconditions','测试数据':'test_data','操作':'steps','操作步骤':'steps','步骤':'steps','预期结果':'expected','预期':'expected','实际结果':'actual','实际':'actual','证据':'evidence'}
SHARED = {'version','device','environment','project'}
EDIT_FIELDS = ('id','selected','title','priority','preconditions','test_data','steps','expected','actual','evidence','module','submodule','problem_type','frequency')
HELP = '''飞书导入，网页详细补充：
1. 发“解析 <执行表链接>”，或直接发送 .xlsx / .zip 文件。
2. 点击回复中的“打开审核与补充页面”。
3. 在页面填写版本、设备、环境，逐条补充分类、四段描述和附件。
4. 保存修改，检查选中条目，点击页面“确认写入”或“确认更新原单”。
发送“审核链接”可重新获取当前批次的链接；恢复已有批次可发“使用 <批次前8位>”。
手机和电脑需连接服务所在的局域网，服务电脑须保持运行。
问题类型、子模块、发生概率按条推断。概率无依据时填 Very High / Frequent，子模块无法确定时取模块名。'''


def assignments(text):
    pattern = r'(' + '|'.join(sorted(map(re.escape, LABELS),key=len,reverse=True)) + r')\s*[=＝:：]\s*'
    matches = list(re.finditer(pattern,text))
    if not matches:
        raise InputError('请使用“字段=内容”，例如：修改 3 发生概率=偶现')
    if text[:matches[0].start()].strip():
        raise InputError('无法识别字段，请发送“帮助”查看示例。')
    return {LABELS[match[1]]:text[match.end():matches[index+1].start() if index+1<len(matches) else len(text)].strip().strip('"') for index,match in enumerate(matches)}


def safe(text):
    return str(text).replace('<','〈').replace('>','〉')


class ChatController:
    def __init__(self, settings, store, client, service):
        self.settings,self.store,self.client,self.service=settings,store,client,service

    def say(self, chat_id, title, content):
        content=safe(content)
        if hasattr(self.client,'send_card') and len(content)<7000:
            try:
                self.client.send_card(chat_id,title,content)
                return
            except ValueError as exc:
                LOGGER.warning('卡片发送失败，改用文字回复：%s',exc)
        text=title+'\n'+content
        for start in range(0,len(text),6000):
            self.client.send_text(chat_id,text[start:start+6000])

    def remember(self, chat_id, user_id, batch, **extra):
        session=self.store.chat_session(chat_id,user_id)
        session.update(batch_id=batch['id'],page=1)
        session.pop('attachment_index',None)
        session.pop('pending_import',None)
        session.update(extra)
        self.store.save_chat_session(chat_id,user_id,session)

    def batch(self, chat_id, user_id):
        session=self.store.chat_session(chat_id,user_id)
        if session.get('pending_import'):
            raise InputError('上次导入未完成。请重新导入，或发送“使用 <原批次ID>”明确恢复旧批次。')
        if not session.get('batch_id'):
            raise InputError('当前聊天没有待审批次。请先发执行表链接或 .xlsx/.zip 文件。')
        batch=self.service.review(session['batch_id'])
        if batch['defaults'].get('reporter')!=user_id:
            raise InputError('此批次的提交人不是当前账号，请使用本人发起的批次。')
        return batch

    def show(self, chat_id, user_id):
        batch=self.batch(chat_id,user_id)
        incomplete=sum(bool(item.get('issues')) or any(batch.get('default_hints',{}).get(key,{}).get('enabled') and not item.get(key) for key in ('problem_type','module','submodule','frequency')) for item in batch['items'])
        attachments=sum(len(item.get('attachments',[])) for item in batch['items'])
        existing=sum(bool(item.get('record_id')) for item in batch['items'])
        lines=[f"**{batch['title']}**", f"批次 {batch['id'][:8]} · 失败 {len(batch['items'])} 条 · 待补充 {incomplete} 条 · 附件 {attachments} 个",
               f"新建 {len(batch['items'])-existing} 条 / 更新原单 {existing} 条",
               f"\n[打开审核与补充页面]({review_url(self.settings,batch['id'])})",
               '\n在页面补充版本、设备、分类、四段描述和附件，保存后再确认写入。已有缺陷更新原单。']
        if batch.get('warnings'):
            lines.append(f"\n有 {len(batch['warnings'])} 条附件导入提示，请在页面展开检查。")
        lines.append('\n链接仅用于本批次，24 小时内有效；过期可回复“审核链接”。请在同一局域网内打开。')
        self.say(chat_id,'待补充 · 打开网页审核','\n'.join(lines))

    def save(self,batch,changes=None,defaults=None):
        edits=[]
        for index,item in enumerate(batch['items'],1):
            edit={key:item.get(key,'') for key in EDIT_FIELDS}
            edit.update((changes or {}).get(index,{}))
            edits.append(edit)
        body=ReviewUpdate(revision=batch['revision'],defaults={**batch['defaults'],**(defaults or {})},items=edits)
        return self.service.update(batch['id'],body)

    def handle_text(self,chat_id,text,user_id):
        if text in {'帮助','help'}:
            self.say(chat_id,'Fail2Bug 使用方法',HELP);return
        if text.startswith('解析'):
            # 飞书文档预览可将链接表示为 [标题](URL)，右括号属于消息格式。
            match = re.search(r'''https://[^\s<>()\[\]"'（）]+''', text)
            if not match:
                raise InputError('发送“解析 <飞书电子表格链接>”，或直接发送 .xlsx/.zip 文件。')
            self.client.send_text(chat_id,'已收到，正在读取和推断分类。')
            session=self.store.chat_session(chat_id,user_id);session['pending_import']=True;self.store.save_chat_session(chat_id,user_id,session)
            batch=self.service.import_sheet(match[0],reporter=user_id)
            self.remember(chat_id,user_id,batch);self.show(chat_id,user_id);return
        if text.startswith('使用 '):
            prefix=text.split(maxsplit=1)[1].strip()
            matches=[batch for batch in self.store.recent(1000) if batch['id'].startswith(prefix) and batch['defaults'].get('reporter')==user_id]
            if len(prefix)<8 or len(matches)!=1:
                raise InputError('请提供本人批次的完整 ID 或至少前 8 位，且应唯一匹配。')
            self.remember(chat_id,user_id,matches[0]);self.show(chat_id,user_id);return
        batch=self.batch(chat_id,user_id)
        session=self.store.chat_session(chat_id,user_id)
        if text=='告警':
            self.say(chat_id,'导入提示','\n'.join(batch.get('warnings',[])) or '没有附件导入告警。');return
        if text in {'查看','清单','上一页','下一页','审核链接','打开网页','补充'}:
            self.show(chat_id,user_id);return
        match=re.fullmatch(r'详情\s*(\d+)',text)
        if match:
            index=int(match[1]);item=self.item(batch,index)
            inference='\n'.join(f'{label}：{item.get(key) or "待补充"}（{item.get("inference",{}).get(key,{}).get("reason","人工填写")}）' for key,label in [('problem_type','问题类型'),('submodule','子模块'),('frequency','发生概率')])
            self.say(chat_id,f'#{index} {item["case_id"]}',item['title']+'\n\n'+inference+'\n\n'+format_repro(item)+f"\n\n附件 {len(item.get('attachments',[]))} 个。\n[打开网页详细补充]({review_url(self.settings,batch['id'])})")
            return
        if text.startswith('设置 '):
            values=assignments(text.split(maxsplit=1)[1])
            if set(values)-SHARED:
                raise InputError('分类没有批次默认。请用“修改 3 问题类型=... 发生概率=...”逐条补充。')
            values={key:value.replace('、','\n') if key in {'device','environment'} else value for key,value in values.items()}
            self.save(batch,defaults=values);self.show(chat_id,user_id);return
        match=re.match(r'修改\s*(\d+)\s+(.+)',text,re.S)
        if match:
            index=int(match[1]);self.item(batch,index)
            values=assignments(match[2])
            if 'priority' in values:values['priority']=values['priority'].upper()
            if set(values)&SHARED:
                raise InputError('版本、设备和环境请用“设置”命令填写。')
            if 'frequency' in values:
                raw=values['frequency'];allowed=batch.get('default_hints',{}).get('frequency',{}).get('options',[])
                value,_=infer_frequency('发生概率：'+raw,allowed)
                if raw and not value:
                    raise InputError('发生概率请填“必现 / 高概率 / 偶现 / 低概率”，或目标表的完整选项。')
                values['frequency']=value
            for key in {'module','problem_type'}&set(values):
                values[key]=values[key].replace('、','\n')
            self.save(batch,changes={index:values});self.show(chat_id,user_id);return
        match=re.fullmatch(r'(忽略|选择)\s*([\d,，、 ]+)',text)
        if match:
            indexes=[int(value) for value in re.split(r'[,，、 ]+',match[2]) if value]
            for index in indexes:self.item(batch,index)
            self.save(batch,changes={index:{'selected':match[1]=='选择'} for index in indexes});self.show(chat_id,user_id);return
        match=re.fullmatch(r'附件\s*(\d+)',text)
        if match:
            index=int(match[1]);self.item(batch,index)
            session['attachment_index']=index;self.store.save_chat_session(chat_id,user_id,session)
            self.say(chat_id,'发送证据附件',f'下一张图片、视频或文件将关联到第 {index} 条。发送“取消附件”可取消。');return
        if text=='取消附件':
            session.pop('attachment_index',None);self.store.save_chat_session(chat_id,user_id,session)
            self.say(chat_id,'已取消','未改变已有附件。');return
        if text in {'确认提交','确认更新','确认写入'}:
            result=self.service.submit(batch['id'],batch['revision'])
            lines=[f"批次 {batch['id'][:8]}"]
            for index,item in enumerate(result['items'],1):
                if not item['selected']:continue
                lines.append(f"#{index} {item['case_id']}：{item['status']}"+(f"\n{item['record_url']}" if item.get('record_url') else '')+(f"\n{item['error']}" if item.get('error') else ''))
            self.say(chat_id,'写入结果','\n'.join(lines));return
        match=re.match(r'选项\s+(问题类型|模块|子模块|发生概率|设备|环境)\s*(.*)',text)
        if match:
            key=LABELS[match[1]];query=match[2].strip()
            options=batch.get('default_hints',{}).get(key,{}).get('options',[])
            matches=[value for value in options if query.lower() in value.lower()]
            self.say(chat_id,match[1]+'选项','\n'.join(matches[:40])+(f'\n共 {len(matches)} 项，可追加关键词缩小范围。' if len(matches)>40 else ''));return
        raise InputError('未识别命令，请发送“帮助”。')

    def item(self,batch,index):
        if not 1<=index<=len(batch['items']):raise InputError('条目序号超出范围。')
        return batch['items'][index-1]

    def handle_file(self,chat_id,user_id,message_id,content,message_type):
        name=Path(content.get('file_name') or ('截图.png' if message_type=='image' else '录屏.mp4' if message_type=='media' else '附件')).name
        session=self.store.chat_session(chat_id,user_id)
        key=content.get('image_key') if message_type=='image' else content.get('file_key')
        if not key:raise InputError('没有收到可下载的文件，请以文件或图片消息重新发送。')
        with TemporaryDirectory(prefix='f2b-chat-') as folder:
            path=Path(folder)/name
            if path.suffix.lower() in {'.xlsx','.zip'} and not session.get('attachment_index'):
                session['pending_import']=True;self.store.save_chat_session(chat_id,user_id,session)
            self.client.download_message_file(message_id,key,path,'image' if message_type=='image' else 'file')
            if path.suffix.lower() in {'.xlsx','.zip'} and not session.get('attachment_index'):
                batch=self.service.import_file(path,reporter=user_id)
                self.remember(chat_id,user_id,batch);self.show(chat_id,user_id);return
            if not session.get('attachment_index'):
                raise InputError('要给缺陷补附件，请先发送“附件 3”，再发送图片、视频或证据文件。')
            batch=self.batch(chat_id,user_id);item=self.item(batch,session['attachment_index'])
            from .local_source import MEDIA_EXTENSIONS
            if path.suffix.lower() not in MEDIA_EXTENSIONS:
                raise InputError('请发送图片、视频、PDF、TXT 或日志文件。')
            storage_name=uuid4().hex;target=self.service.attachment_path(storage_name);target.parent.mkdir(parents=True,exist_ok=True)
            import shutil
            shutil.copyfile(path,target);target.chmod(0o600)
            entry={'id':str(uuid4()),'name':name,'size':path.stat().st_size,'content_type':mimetypes.guess_type(name)[0] or 'application/octet-stream','storage_name':storage_name,'linked':False}
            try:
                self.service.add_attachment(batch['id'],item['id'],entry,batch['revision'])
            except BaseException:
                target.unlink(missing_ok=True);raise
            session.pop('attachment_index',None);self.store.save_chat_session(chat_id,user_id,session)
            self.say(chat_id,'证据已加入草稿',f"已关联第 {batch['items'].index(item)+1} 条：{name}。\n[打开网页检查并确认写入]({review_url(self.settings,batch['id'])})")
