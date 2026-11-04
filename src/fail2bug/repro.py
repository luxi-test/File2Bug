import re


def repro_parts(item: dict) -> tuple[str, str]:
    steps = str(item.get('steps', '') or '')
    premise = str(item.get('preconditions', '') or '')
    test_data = str(item.get('test_data', '') or '')
    # 兼容此前保存的“前置条件/测试数据/操作步骤”合并草稿。
    if steps.startswith(('前置条件\n', '测试数据\n', '操作步骤\n')):
        parts = re.split(r'(?:^|\n\n)(前置条件|测试数据|操作步骤)\n', steps)
        found = {parts[index]: parts[index + 1].strip() for index in range(1, len(parts) - 1, 2)}
        premise = premise or found.get('前置条件', '')
        test_data = test_data or found.get('测试数据', '')
        steps = found.get('操作步骤', steps)
    if test_data:
        premise = (premise + '\n' if premise else '') + '测试数据：' + test_data
    return premise.strip(), steps.strip()


def format_repro(item: dict) -> str:
    premise, steps = repro_parts(item)
    return '\n\n'.join(f'【{label}】：\n{value}' for label, value in [
        ('前提', premise), ('操作', steps),
        ('预期结果', str(item.get('expected', '') or '').strip()),
        ('实际结果', str(item.get('actual', '') or '').strip()),
    ])
