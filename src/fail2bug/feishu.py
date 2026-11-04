import threading
import time
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse, urlencode, urlunparse

import httpx

from .config import Settings
from .parser import InputError


class FeishuError(ValueError):
    pass


class SubmissionUnknown(FeishuError):
    """网络结果不明确，不能断言服务端没有写入。"""


def parse_sheet_url(url: str) -> tuple[str, str | None]:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "feishu.cn" or host.endswith(".feishu.cn")) or parsed.username or parsed.password:
        raise InputError("请输入 https://xxx.feishu.cn/sheets/... 格式的执行表链接。")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "sheets" or not parts[1].isalnum():
        raise InputError("首版支持飞书电子表格 sheets 链接，请在原表中复制链接。")
    query = parse_qs(parsed.query)
    if parsed.fragment:
        query.update(parse_qs(parsed.fragment))
    return parts[1], query.get("sheet", query.get("sheet_id", [None]))[0]


def column_name(number: int) -> str:
    result = ""
    while number:
        number, rest = divmod(number - 1, 26)
        result = chr(65 + rest) + result
    return result


class FeishuClient:
    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.http = httpx.Client(timeout=30, transport=transport)
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def close(self):
        self.http.close()

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            try:
                response = self.http.post(self.BASE_URL + "/auth/v3/tenant_access_token/internal", json={"app_id": self.settings.app_id, "app_secret": self.settings.app_secret.get_secret_value()})
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise FeishuError("无法获取飞书应用凭证，请检查网络及应用配置。") from exc
            if data.get("code", 0) != 0 or not data.get("tenant_access_token"):
                raise FeishuError(f"获取应用凭证失败（错误码 {data.get('code', '未知')}），请检查 APP_ID 和 APP_SECRET。")
            self._token = data["tenant_access_token"]
            self._expires = time.monotonic() + max(0, int(data.get("expire", 7200)) - 300)
            return self._token

    def _request(self, method: str, path: str, *, json=None, params=None, writing=False, data=None, files=None):
        token = self._access_token()
        for attempt in range(3):
            try:
                body = {"data": data, "files": files} if files is not None else {"json": json}
                if files:
                    for value in files.values():
                        stream = value[1] if isinstance(value, tuple) else value
                        if hasattr(stream, "seek"):
                            stream.seek(0)
                response = self.http.request(method, self.BASE_URL + path, **body, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                error = SubmissionUnknown if writing else FeishuError
                raise error("飞书请求连接中断，请检查网络后重试。" if not writing else "未收到飞书的完整响应，提交结果待确认；重试将沿用同一操作编号。") from exc
            if response.status_code >= 500:
                error = SubmissionUnknown if writing else FeishuError
                raise error(f"飞书服务暂时不可用（HTTP {response.status_code}），请稍后重试。")
            try:
                body = response.json()
            except ValueError as exc:
                error = SubmissionUnknown if writing else FeishuError
                raise error("飞书返回了无法识别的响应，请稍后重试。") from exc
            code = body.get("code", 0)
            if writing and code in {1255001, 1255002, 1255003, 1255004, 1255005, 1255040}:
                raise SubmissionUnknown(f"飞书处理异常（错误码 {code}），提交结果待确认；请沿用原操作重试。")
            if response.status_code == 429 or code == 1254290:
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
            if not response.is_success or code != 0:
                if code == 99991672:
                    match = re.search(r"scopes is required:\s*\[([^\]]+)\]", str(body.get("msg", "")))
                    scopes = [] if not match else [scope.strip() for scope in match[1].split(",") if re.fullmatch(r"[a-zA-Z0-9_:.-]+", scope.strip())]
                    if scopes:
                        raise FeishuError("应用尚未开通所需的应用身份权限，请开通以下任一权限并发布应用版本：" + "、".join(scopes) + "。权限入口：https://open.feishu.cn/app/" + self.settings.app_id + "/auth")
                    raise FeishuError("飞书拒绝访问（99991672）。请检查应用身份权限是否已开通并发布，以及应用是否已添加为该表的协作者。")
                if code in {91403, 1254302}:
                    raise FeishuError(f"应用没有访问这张表所需的文档权限（{code}）。请将应用添加为表格协作者；执行表需要阅读权限，缺陷表需要编辑权限。")
                # 不回显整段响应，避免日志或页面泄漏服务端返回的敏感字段。
                raise FeishuError(f"飞书请求失败（HTTP {response.status_code}，错误码 {code}）。请检查字段类型、应用权限及协作者设置。")
            # 机器人信息接口的 bot 位于响应顶层，其余接口通常使用 data。
            return body.get("data", body)
        raise FeishuError("飞书请求频率过高，请稍后重试。")

    def read_sheet(self, url: str) -> tuple[list[list], str, str]:
        token, selected = parse_sheet_url(url)
        data = self._request("GET", f"/sheets/v3/spreadsheets/{token}/sheets/query")
        sheets = [sheet for sheet in data.get("sheets", []) if sheet.get("resource_type", "sheet") == "sheet"]
        sheet = next((value for value in sheets if value["sheet_id"] == selected), None) if selected else next(iter(sorted(sheets, key=lambda value: value.get("index", 0))), None)
        if not sheet:
            raise InputError("没有找到指定工作表，请检查链接中的 sheet 参数。")
        grid = sheet.get("grid_properties", {})
        row_count, column_count = grid.get("row_count"), grid.get("column_count")
        if not row_count or not column_count:
            raise InputError("未取得完整工作表范围，暂未导入，请检查工作表类型。")
        if row_count > self.settings.max_rows or column_count > self.settings.max_columns:
            raise InputError(f"工作表网格超过首版上限（{self.settings.max_rows} 行 / {self.settings.max_columns} 列），请复制所需范围到单独工作表。此次未截断导入。")
        rows = []
        # 控制每次读取单元格数，分页读完全部网格，不在空白行处提前结束。
        page_rows = max(1, min(500, 5000 // column_count))
        for start in range(1, row_count + 1, page_rows):
            stop = min(start + page_rows - 1, row_count)
            range_name = f"{sheet['sheet_id']}!A{start}:{column_name(column_count)}{stop}"
            data = self._request("GET", f"/sheets/v2/spreadsheets/{token}/values/{quote(range_name, safe='')}", params={"valueRenderOption": "ToString"})
            if "valueRange" not in data:
                raise InputError("飞书未返回完整单元格范围，导入已停止，请重新尝试。")
            values = data["valueRange"].get("values", [])
            if len(values) > stop - start + 1:
                raise InputError("飞书返回的行数与请求范围不符，导入已停止。")
            rows.extend(values)
            rows.extend([[] for _ in range(stop - start + 1 - len(values))])
        canonical = f"https://{urlparse(url).hostname}/sheets/{token}?sheet={sheet['sheet_id']}"
        return rows, sheet.get("title", "执行表"), canonical

    def verify_fields(self):
        settings = self.settings
        fields, page_token = {}, None
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", f"/bitable/v1/apps/{settings.target_app_token}/tables/{settings.target_table_id}/fields", params=params)
            fields.update({field["field_name"]: field for field in data.get("items", [])})
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                raise FeishuError("缺陷表字段分页响应不完整，请稍后重试。")
        result = {}
        for logical, name in settings.fields.items():
            field = fields.get(name)
            expected = {1, 3} if logical in {"priority", "status", "project", "submodule", "frequency"} else {1, 4} if logical in {"device", "environment", "problem_type", "module"} else {1, 11} if logical == "reporter" else {17} if logical == "attachments" else {1}
            if field is None:
                raise FeishuError(f"缺陷表缺少字段「{name}」，请调整表结构或 .env 中的字段映射。")
            if field["type"] not in expected:
                raise FeishuError(f"字段「{name}」类型不匹配：不支持将{logical}写入此类型（{field['type']}）。")
            if logical == "priority" and field["type"] == 3:
                names = {option["name"] for option in field.get("property", {}).get("options", [])}
                if not {"P0", "P1", "P2", "P3"}.issubset(names):
                    raise FeishuError(f"单选字段「{name}」需要 P0、P1、P2、P3 四个选项。")
            if logical == "status" and field["type"] == 3:
                names = {option["name"] for option in field.get("property", {}).get("options", [])}
                if settings.initial_status not in names:
                    raise FeishuError(f"状态字段缺少选项「{settings.initial_status}」。")
            result[name] = field
        return result

    def create_record(self, fields: dict, operation_id: str) -> dict:
        settings = self.settings
        result = self._request("POST", f"/bitable/v1/apps/{settings.target_app_token}/tables/{settings.target_table_id}/records", json={"fields": fields}, params={"client_token": operation_id}, writing=True)
        record_id = result.get("record", {}).get("record_id")
        if not record_id:
            raise SubmissionUnknown("飞书未返回缺陷记录编号，结果待确认。")
        url = urlparse(settings.target_url or f"https://feishu.cn/base/{settings.target_app_token}")
        query = parse_qs(url.query)
        query.update(table=[settings.target_table_id], record=[record_id])
        record_url = urlunparse(url._replace(query=urlencode(query, doseq=True)))
        return {"record_id": record_id, "record_url": record_url}

    def get_record(self, record_id: str) -> dict:
        s = self.settings
        return self._request("GET", f"/bitable/v1/apps/{s.target_app_token}/tables/{s.target_table_id}/records/{record_id}")["record"]

    def update_record(self, record_id: str, fields: dict, operation_id: str) -> dict:
        s = self.settings
        result = self._request("PUT", f"/bitable/v1/apps/{s.target_app_token}/tables/{s.target_table_id}/records/{record_id}", json={"fields": fields}, writing=True)
        if result.get("record", {}).get("record_id") != record_id:
            raise SubmissionUnknown("飞书未确认原缺陷更新结果，请重试原操作。")
        return {"record_id": record_id}

    def upload_media(self, path: Path, name: str, content_type: str) -> str:
        size = path.stat().st_size
        common = {"file_name": name, "parent_type": "bitable_file", "parent_node": self.settings.target_app_token, "size": size}
        if size <= 20 * 1024 * 1024:
            with path.open("rb") as handle:
                result = self._request("POST", "/drive/v1/medias/upload_all", data={key: str(value) for key, value in common.items()}, files={"file": (name, handle, content_type)})
        else:
            prepared = self._request("POST", "/drive/v1/medias/upload_prepare", json=common)
            upload_id, block_size, block_num = prepared["upload_id"], prepared["block_size"], prepared["block_num"]
            if not isinstance(block_size, int) or block_size <= 0 or block_num != (size + block_size - 1) // block_size:
                raise FeishuError("飞书返回的分片参数不完整，附件尚未绑定到缺陷。")
            with path.open("rb") as handle:
                for seq in range(block_num):
                    block = handle.read(block_size)
                    self._request("POST", "/drive/v1/medias/upload_part", data={"upload_id": upload_id, "seq": str(seq), "size": str(len(block))}, files={"file": (name, block, content_type)})
            result = self._request("POST", "/drive/v1/medias/upload_finish", json={"upload_id": upload_id, "block_num": block_num})
        if not result.get("file_token"):
            raise FeishuError("飞书未返回附件编号，附件尚未绑定到缺陷。")
        return result["file_token"]

    def send_text(self, chat_id: str, text: str):
        import json
        return self._request("POST", "/im/v1/messages", params={"receive_id_type": "chat_id"}, json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)})

    def send_card(self, chat_id: str, title: str, content: str):
        import json
        card = {'schema':'2.0','header':{'title':{'tag':'plain_text','content':title},'template':'blue'},'body':{'elements':[{'tag':'markdown','content':content}]}}
        return self._request('POST','/im/v1/messages',params={'receive_id_type':'chat_id'},json={'receive_id':chat_id,'msg_type':'interactive','content':json.dumps(card,ensure_ascii=False)})

    def download_message_file(self, message_id: str, file_key: str, destination: Path, resource_type='file'):
        token = self._access_token()
        url = self.BASE_URL + f'/im/v1/messages/{quote(message_id,safe="")}/resources/{quote(file_key,safe="")}'
        try:
            with self.http.stream('GET',url,params={'type':resource_type},headers={'Authorization':f'Bearer {token}'}) as response:
                if not response.is_success or 'application/json' in response.headers.get('content-type',''):
                    response.read()
                    try:
                        code = response.json().get('code','未知')
                    except ValueError:
                        code = response.status_code
                    raise FeishuError(f'下载聊天文件失败（{code}）。请检查接收消息和读取消息资源的应用权限。')
                total = 0
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as handle:
                    destination.chmod(0o600)
                    for chunk in response.iter_bytes(1024*1024):
                        total += len(chunk)
                        if total > self.settings.max_import_mb * 1024 * 1024:
                            raise FeishuError('聊天文件超过导入大小上限。')
                        handle.write(chunk)
                if not total:
                    raise FeishuError('下载到的聊天文件为空。')
        except (httpx.HTTPError, FeishuError) as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, FeishuError):
                raise
            raise FeishuError('下载聊天文件时连接中断，请重新发送文件。') from exc

    def bot_open_id(self) -> str:
        data = self._request("GET", "/bot/v3/info")
        open_id = data.get("bot", {}).get("open_id")
        if not open_id:
            raise FeishuError("未取得机器人身份，请检查应用是否已启用机器人并发布。")
        return open_id
