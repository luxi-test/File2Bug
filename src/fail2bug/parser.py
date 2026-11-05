import unicodedata
from uuid import uuid4

from .models import Draft, draft_issues


class InputError(ValueError):
    pass


HEADERS = {
    "用例ID": "case_id", "PRD模块": "module", "标题": "title", "优先级": "priority",
    "前置条件": "preconditions", "测试数据": "test_data", "操作步骤": "steps",
    "预期结果": "expected", "执行结果": "result", "实际结果": "actual", "备注": "remark", "证据": "evidence",
    "需求点": "source_feature",
    "发生概率": "frequency_evidence", "复现概率": "frequency_evidence",
    "复现次数": "reproductions", "执行次数": "attempts",
}
REQUIRED = {"用例ID", "PRD模块", "标题", "优先级", "操作步骤", "预期结果", "执行结果"}
HEADER_ALIASES = {
    "用例编号": "用例ID",
    "所属模块": "PRD模块",
    "用例标题": "标题",
    "测试步骤": "操作步骤",
}
ALIASES = {
    "pass": {"pass", "passed", "p", "ok", "通过", "成功", "已通过"},
    "fail": {"fail", "failed", "f", "ng", "nok", "失败", "不通过", "未通过"},
    "blocked": {"blocked", "block", "阻塞", "受阻", "环境问题"},
    "skip": {"skip", "skipped", "跳过", "不执行", "忽略"},
    "not_run": {"notrun", "not run", "未执行", "待执行", ""},
}


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(cell_text(part) for part in value)
    if isinstance(value, dict):
        if "text" in value:
            return cell_text(value["text"])
        if "link" in value:
            return str(value["link"])
        if "value" in value:
            return cell_text(value["value"])
        return ""
    return str(value)


def clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).replace("\u200b", "").replace("\ufeff", "").strip().split())


def column_header(value) -> str:
    header = clean(cell_text(value))
    return HEADER_ALIASES.get(header, header)


def normalize_result(value) -> str:
    text = clean(cell_text(value)).lower()
    # 仅处理明确的双语标签，避免把“通过(但失败)”错误识别成通过。
    bilingual = {"失败(fail)": "fail", "通过(pass)": "pass", "阻塞(blocked)": "blocked", "跳过(skip)": "skip"}
    text = text.replace(" (", "(")
    if text in bilingual:
        return bilingual[text]
    for result, names in ALIASES.items():
        if text in names:
            return result
    return "unknown"


def parse_table(rows: list[list], source_url: str) -> dict:
    if not rows:
        raise InputError("表格为空，请检查执行表是否有表头和用例。")
    scores = [sum(column_header(cell) in HEADERS for cell in row) for row in rows[:10]]
    header_at = max(range(len(scores)), key=scores.__getitem__)
    headers = [column_header(value) for value in rows[header_at]]
    missing = sorted(REQUIRED - set(headers))
    if missing:
        raise InputError("执行表缺少列：" + "、".join(missing))
    duplicate = [name for name in HEADERS if headers.count(name) > 1]
    if duplicate:
        raise InputError("表头重复，请保留唯一列：" + "、".join(duplicate))
    drafts, skipped = [], []
    counts = {key: 0 for key in [*ALIASES, "unknown"]}
    for row_no, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        if not any(cell_text(cell).strip() for cell in row):
            continue
        values = {HEADERS[header]: cell_text(row[index]).strip() if index < len(row) else "" for index, header in enumerate(headers) if header in HEADERS}
        result = normalize_result(values.get("result", ""))
        counts[result] += 1
        if result != "fail":
            skipped.append({"row_no": row_no, "case_id": values.get("case_id", ""), "result": result, "raw_result": values.get("result", "")})
            continue
        title = values.get("title", "")
        module = values.get("module", "")
        item = Draft(
            id=str(uuid4()), case_id=values.get("case_id", ""), row_no=row_no,
            title=f"[{module}] {title}" if module and title else title,
            module=module, source_feature=values.get("source_feature", ""), priority=clean(values.get("priority", "")).upper(),
            preconditions=values.get("preconditions", ""), test_data=values.get("test_data", ""),
            steps=values.get("steps", ""), frequency_evidence=values.get("frequency_evidence", ""),
            reproductions=values.get("reproductions", ""), attempts=values.get("attempts", ""),
            expected=values.get("expected", ""), actual=values.get("actual") or values.get("remark", ""), evidence=values.get("evidence", ""),
        ).model_dump()
        item["issues"] = draft_issues(item)
        if not item["case_id"]:
            item["issues"].append("源表缺少用例ID，请先补齐源表后重新导入")
        drafts.append(item)
    return {"items": drafts, "counts": counts, "skipped": skipped, "source_url": source_url}


def demo_table() -> list[list[str]]:
    headers = ["用例ID", "PRD模块", "标题", "优先级", "前置条件", "测试数据", "操作步骤", "预期结果", "执行结果", "备注", "证据"]
    return [headers,
        ["TC-021", "设备连接", "重新连接后设备名称未更新", "P1", "设备已绑定，蓝牙已开启", "设备名称：我的眼镜", "1. 修改设备名称\n2. 断开蓝牙后重新连接\n3. 查看设备首页", "显示修改后的设备名称", "失败（Fail）", "重新连接后仍显示旧名称，退出页面再进入才更新。", "设备名称未更新.mp4"],
        ["TC-034", "账号登录", "验证码输入错误时缺少提示", "P2", "进入手机号登录页", "验证码：000000", "1. 输入手机号获取验证码\n2. 输入错误验证码\n3. 点击登录", "提示验证码错误，并允许重新输入", "fail", "点击后按钮恢复可用，但没有错误提示。", ""],
        ["TC-052", "消息通知", "关闭推送后仍收到提醒", "P1", "账号已登录，系统通知权限已开启", "", "1. 关闭应用内推送开关\n2. 触发一条设备离线通知", "不再接收推送提醒", "FAIL", "关闭应用内推送后仍收到离线提醒。", "推送设置截图.png"],
        ["TC-067", "设备连接", "固件升级中断后进度展示异常", "P2", "设备正在升级固件", "", "1. 开始固件升级\n2. 升级中关闭蓝牙\n3. 重新开启蓝牙", "提示升级中断，并允许重新升级", "失败", "", ""],
        ["TC-075", "个人设置", "修改头像后首页未同步", "P3", "账号已登录", "一张 PNG 图片", "1. 打开个人资料\n2. 修改头像\n3. 返回首页", "首页同步显示新头像", "fail", "资料页头像更新成功，首页仍显示旧头像。", ""],
        ["TC-080", "账号登录", "手机号正常登录", "P1", "", "", "输入验证码登录", "进入首页", "pass", "", ""],
        ["TC-081", "设备连接", "搜索附近设备", "P1", "", "", "点击搜索", "展示设备列表", "blocked", "测试设备未就绪", ""],
        ["TC-082", "消息通知", "收到设备告警", "P2", "", "", "触发告警", "收到通知", "⭕", "结果符号尚未约定", ""],
    ]
