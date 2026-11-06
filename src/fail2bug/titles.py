"""用执行记录中的事实组织缺陷标题，不把预期结果反写成异常。"""
import re

_PLACEHOLDER = re.compile(
    r"(?:fail(?:ed)?|pass(?:ed)?|失败|不通过|未通过|通过|异常|不正常|不生效|已提单|已提交|"
    r"见(?:附件|截图|视频|录屏|备注)|详见(?:附件|截图|视频|录屏|备注)|如图|同上|待补充|暂无|无|"
    r"未复现|无法复现|未能复现|未发现(?:问题|异常)|没有(?:发现)?(?:问题|异常)|无异常|一切正常|正常|n/?a|none|tbd)[。.!！]*", re.I)
_SYMPTOM = re.compile(
    r"无法|不能|没有|缺少|未|仍|失败|错误|异常|无响应|无反应|崩溃|闪退|卡死|卡顿|丢失|"
    r"空白|遮挡|重叠|溢出|错位|不一致|不生效|不同步|不更新|打不开|crash|\bANR\b", re.I)


def title_without_module(title: str, module: str) -> str:
    if module:
        for prefix in (f"[{module}]", f"【{module}】"):
            if title.startswith(prefix):
                return title[len(prefix):].lstrip()
    return title


def _has_symptom(text):
    text = re.sub(r"(?:没有|未发生|不再|不会|未)(?:出现)?(?:崩溃|闪退|卡死)", "", text)
    return bool(_SYMPTOM.search(text))


def _context(case_title):
    text = re.sub(r"^(?:(?:验证|检查|确认|校验)(?:[：:\s]+|(?=登录|退出|打开|关闭|点击|修改|删除|添加|切换|返回|提交|保存|搜索|绑定|连接|更新)))+", "", case_title)
    temporal = re.match(r"^(.{2,36}?(?:之后|以后|之前|以前|的时候|时候|后(?!台|退|续|端|置|缀)|前(?!台|端|置|景|往|缀)|时(?!间|长|区|钟|效|刻|序|延)))", text)
    if temporal:
        return temporal[1]
    text = re.sub(r"(?:是否|能否).*$", "", text)
    text = re.sub(r"(?:功能正常|显示正常|展示正确|显示正确|正常显示|正常展示|正常工作|正确适配|成功)$", "", text)
    return text.strip(' ：:，,。?？')[:40]


def suggest_title(case_title: str, actual: str, module: str = "") -> dict:
    case_title = title_without_module(str(case_title).strip(), module)
    descriptions = []
    for part in re.split(r"[\n。；;！？!?]+|(?<=[.!?])\s+", str(actual or '')):
        text = re.sub(r"^(?:\d+[.、)）]\s*|[-•]\s*)", "", part.strip())
        text = re.sub(r"^(?:【实际结果】[：:]?|实际结果[：:]|实际[：:])\s*", "", text)
        text = re.sub(r"^(?:已提单|已提交|失败|fail)[，,：:\s]+", "", text, flags=re.I)
        text = ' '.join(text.split()).strip(' 。；;')
        if not text or _PLACEHOLDER.fullmatch(text) or re.fullmatch(r"https?://\S+|(?:[^\s]+\.(?:png|jpe?g|webp|mp4|mov))", text, flags=re.I):
            continue
        descriptions.append(text)
    if not descriptions:
        return {'value':case_title[:300], 'source':'case_title', 'needs_review':True,
                'reason':'实际结果缺少具体异常，暂保留用例标题，请补充实际表现或手动改写标题。'}
    summary = next((value for value in descriptions if _has_symptom(value)), descriptions[0])
    clauses = re.split(r'[，,]', summary)
    for index, clause in enumerate(clauses):
        if _has_symptom(clause):
            start = 0
            while start < index and re.search(r'(?:成功|正常|正确)$', clauses[start].strip()):
                start += 1
            summary = '，'.join(clauses[start:index+1])
            break
    summary = title_without_module(summary, module).rstrip('。.!！')
    context = _context(case_title)
    generic = len(summary) <= 4 or re.match(r'^(?:没有|未|无法|不能|仍|还是|不|无响应|无反应|点击后|点击时|它|该功能|这个)', summary)
    if generic and context and context not in summary:
        joiner = '' if context.endswith(('时','后','前','时候')) and re.match(r'^(?:没有|未|无法|不能|仍|不|无)', summary) else '：'
        summary = context + joiner + summary
    if len(summary) > 120:
        summary = summary[:119].rstrip('，,：: ') + '…'
    return {'value':summary, 'source':'actual', 'needs_review':False,
            'reason':'根据实际异常生成；简短描述补充用例场景，不推测原因。'}


def apply_title(item: dict):
    suggestion = suggest_title(item.get('case_title', ''), item.get('actual', ''), item.get('module', ''))
    item['title'] = suggestion['value']
    item.setdefault('inference', {})['title'] = suggestion
