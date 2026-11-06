"""从执行记录推断分类；没有复现依据时使用约定的概率默认值。"""
import re

FREQUENCIES = {
    'very_high': 'Very High / Frequent', 'high': 'High / Likely',
    'medium': 'Medium / Occasional', 'low': 'Low / Rare',
}


def infer_frequency(text: str, allowed: list[str] | None = None) -> tuple[str, str]:
    text = str(text or '').strip()
    allowed = list(FREQUENCIES.values()) if allowed is None else allowed
    for value in allowed:
        if value and value.lower() in text.lower():
            return value, '执行记录明确提供发生概率'
    ratio = re.search(r'(?:复现|重现|失败)\s*(\d+)\s*[/／]\s*(\d+)', text)
    percentage = re.search(r'(?:发生概率|复现率|重现率|失败率)\s*[:：=]?\s*(\d+(?:\.\d+)?)\s*%', text)
    value = None
    if ratio:
        hits, total = map(int, ratio.groups())
        if total > 0 and 0 < hits <= total:
            value = hits / total
    elif percentage and 0 < float(percentage[1]) <= 100:
        value = float(percentage[1]) / 100
    if value is not None:
        key = 'very_high' if value >= .8 else 'high' if value >= .5 else 'medium' if value >= .1 else 'low'
        result = FREQUENCIES[key]
        return (result, '根据明确的复现次数或复现率分档') if result in allowed else ('', '复现概率没有对应的目标选项')
    scrubbed = re.sub(r'(?:不是|并非|无法|不能|不|非)(?:稳定)?(?:复现|必现)', '', text)
    rules = [
        (r'必现|每次(?:都|均)?(?:会|出现|发生|失败)|稳定复现', 'very_high'),
        (r'高概率|经常出现|经常发生|频繁出现', 'high'),
        (r'偶现|偶尔出现|间歇性', 'medium'),
        (r'低概率|罕见|极少出现', 'low'),
    ]
    for pattern, key in rules:
        match = re.search(pattern, scrubbed)
        result = FREQUENCIES[key]
        if match and result in allowed:
            return result, f'执行记录明确提到“{match[0]}”'
    return '', '没有复现次数或频率描述，单条 fail 无法推算发生概率'


def infer_problem_type(item: dict, allowed: list[str]) -> tuple[str, str]:
    actual = str(item.get('actual', '')).strip()
    if not actual or actual.lower() in {'已提单', '已提交', 'fail', '失败', '见附件', '见截图'}:
        return '', '实际表现不足，等待补充'
    # 使用实际表现，而不是预期中的“不应崩溃”等反例。
    actual = re.sub(r'(?:没有|未发生|不再|未)(?:崩溃|闪退|卡死)', '', actual, flags=re.I)
    rules = [
        (r'崩溃|闪退|crash|\bANR\b|卡死', ['稳定性问题', '崩溃闪退']),
        (r'内存泄漏|响应(?:很|过)?慢|卡顿|CPU|耗时\s*\d+|延迟\s*\d+', ['性能异常', '性能问题']),
        (r'DNS|网络连接失败|断网|网络异常', ['网络异常', '功能异常']),
        (r'文案|排版|布局|重叠|遮挡|溢出|错位|未分行|不分行|(?:缺少|没有|未显示).{0,6}提示|显示.{0,6}(?:错误|异常|不全)', ['显示异常', 'UI显示问题', '交互缺陷']),
        (r'(?:按钮|点击|弹窗|切换|返回).{0,16}(?:无反应|无响应|无效|失效|无法)|(?:无法|不能).{0,8}(?:点击|切换|返回)', ['交互异常', '交互缺陷', '功能异常']),
        (r'无法|不能|失败|不生效|未更新|不同步|未保存|没有|未出现|未提示|仍|错误|异常|丢失|不符合|未实现', ['功能异常', '功能错误', '功能缺陷']),
    ]
    for pattern, candidates in rules:
        match = re.search(pattern, actual, flags=re.I)
        if match:
            for candidate in candidates:
                if candidate in allowed:
                    return candidate, f'实际表现包含“{match[0]}”'
    return '', '未找到足够明确的问题类型特征'


def infer_item(item: dict, hints: dict, *, only_blank=True) -> dict:
    reasons = item.setdefault('inference', {})
    allowed = hints.get('problem_type', {}).get('options', [])
    if not only_blank or not item.get('problem_type'):
        value, reason = infer_problem_type(item, allowed)
        item['problem_type'] = value
        reasons['problem_type'] = {'value': value, 'reason': reason, 'source': 'actual'}
    if not only_blank or not item.get('frequency'):
        apply_frequency(item, hints)
    if not only_blank or not item.get('submodule'):
        options = hints.get('submodule', {}).get('options', [])
        feature = item.get('source_feature', '').strip()
        if feature and feature in options:
            value, reason, source = feature, '需求点与子模块选项完全一致', 'feature'
        else:
            title = item.get('case_title') or item.get('title', '')
            matches = sorted({option for option in options if len(option) >= 2 and option in title and option not in item.get('module', '').splitlines()}, key=len, reverse=True)
            if matches and (len(matches) == 1 or len(matches[0]) > len(matches[1])):
                value, reason, source = matches[0], '标题明确包含已有子模块名', 'title'
            else:
                value = item.get('module', '').replace('\n', '、')
                reason, source = '子模块无法确定，按要求使用模块名', 'module_fallback'
        item['submodule'] = value
        reasons['submodule'] = {'value': value, 'reason': reason, 'source': source}
    item['inference_version'] = 1
    return item


def apply_frequency(item: dict, hints: dict) -> None:
    hint = hints.get('frequency', {})
    options = hint.get('options', [])
    allowed = options if hint.get('type') == 3 or options else None if hint.get('enabled') else []
    evidence = ('发生概率：' + str(item['frequency_evidence']) + '\n' if item.get('frequency_evidence') else '') + str(item.get('actual', ''))
    if item.get('reproductions') and item.get('attempts'):
        evidence += f"\n复现 {item['reproductions']}/{item['attempts']}"
    value, reason = infer_frequency(evidence, allowed)
    source = 'execution'
    fallback = FREQUENCIES['very_high']
    if not value and (allowed is None or fallback in allowed):
        value, reason, source = fallback, '缺少可用的复现依据，使用默认值', 'default'
    item['frequency'] = value
    item.setdefault('inference', {})['frequency'] = {'value': value, 'reason': reason, 'source': source}
