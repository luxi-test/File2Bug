"""本机管理配置。数据库只存非密钥设置，新增密钥写入 macOS 钥匙串。"""
import json
import re
import threading
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit, urlunsplit, urlencode

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .config import Settings
from .credentials import Keychain
from .feishu import FeishuClient
from .parser import InputError
from .service import Service
from .store import Conflict, Store


FIELD_LABELS = {
    "title": "缺陷标题", "steps": "操作步骤", "expected": "预期结果", "actual": "实际结果",
    "priority": "缺陷等级", "reporter": "提交人", "version": "测试版本", "case_id": "关联用例",
    "source": "来源链接", "device": "测试设备", "environment": "测试环境", "project": "所属项目",
    "status": "状态", "module": "模块", "submodule": "子模块", "problem_type": "问题类型",
    "frequency": "发生概率", "attachments": "截图或视频", "evidence": "证据原文（可选独立字段）",
}
VALUE_KEYS = (
    "target_url", "app_id", "allowed_users", "bot_enabled", "initial_status", "default_project",
    "default_reporter", "default_reporter_name", "default_environment", "max_rows", "max_columns",
    "max_attachment_mb", "max_import_mb",
)


class ConfigValues(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_url: str = Field(max_length=3000)
    app_id: str = Field(max_length=100)
    allowed_users: str = Field(max_length=10000)
    bot_enabled: bool
    initial_status: str = Field(max_length=100)
    default_project: str = Field(max_length=200)
    default_reporter: str = Field(max_length=100)
    default_reporter_name: str = Field(max_length=100)
    default_environment: str = Field(max_length=200)
    max_rows: int = Field(ge=1, le=100000)
    max_columns: int = Field(ge=1, le=1000)
    max_attachment_mb: int = Field(ge=1, le=200)
    max_import_mb: int = Field(ge=1, le=500)


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    values: ConfigValues
    fields: dict[str, str] = Field(max_length=30)
    app_secret: SecretStr | None = Field(default=None, max_length=512)


def parse_target_url(value: str) -> tuple[str, str, str]:
    try:
        url = urlsplit(value.strip())
        host = (url.hostname or "").lower()
        parts = url.path.strip("/").split("/")
        query = parse_qs(url.query)
        if (url.scheme != "https" or not host.endswith(".feishu.cn") or url.username or url.password
                or url.port not in {None, 443} or len(parts) != 2 or parts[0] != "base"
                or not re.fullmatch(r"[A-Za-z0-9]+", parts[1])):
            raise ValueError
        table = query.get("table", [""])[0]
        if not re.fullmatch(r"tbl[A-Za-z0-9]+", table):
            raise ValueError
    except (ValueError, IndexError):
        raise InputError("请填写飞书多维表格 base 链接，并保留 table=tbl… 参数。") from None
    # 不保存分享凭据、视图或记录参数。
    canonical = urlunsplit(("https", host, f"/base/{parts[1]}", urlencode({"table": table}), ""))
    return parts[1], table, canonical


class Configuration:
    def __init__(self, base: Settings, store: Store, *, keychain=None, client_factory=FeishuClient):
        self.base, self.store = base, store
        self.keychain = keychain if keychain is not None else Keychain()
        self.client_factory = client_factory
        self._lock = threading.RLock()
        self._cached_revision, self._cached_settings = -1, base
        with store.connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS app_settings (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, body TEXT NOT NULL)")

    def _record(self, conn):
        row = conn.execute("SELECT revision, body FROM app_settings WHERE id=1").fetchone()
        return (row[0], json.loads(row[1])) if row else (0, {})

    def revision(self):
        with self.store.connection() as conn:
            row = conn.execute("SELECT revision FROM app_settings WHERE id=1").fetchone()
        return row[0] if row else 0

    def snapshot(self) -> tuple[int, Settings]:
        with self._lock:
            with self.store.connection() as conn:
                revision, saved = self._record(conn)
            if revision != self._cached_revision:
                values = {**self.base.model_dump(), **saved.get("overrides", {})}
                if saved.get("secret_ref"):
                    values["app_secret"] = SecretStr(self.keychain.get(saved["secret_ref"]))
                self._cached_settings = Settings(_env_file=None, **values)
                self._cached_revision = revision
            return revision, self._cached_settings

    def public(self):
        revision, settings = self.snapshot()
        return {
            "revision": revision, "mode": settings.mode,
            "values": {key: getattr(settings, key) for key in VALUE_KEYS},
            "fields": {key: getattr(settings, "field_" + key) for key in FIELD_LABELS},
            "secrets": {"app_secret_configured": bool(settings.app_secret.get_secret_value()),
                        "access_key_configured": bool(settings.access_key.get_secret_value())},
        }

    def candidate(self, body: ConfigUpdate) -> Settings:
        revision, current = self.snapshot()
        if revision != body.revision:
            raise Conflict("配置已在另一个页面修改，请刷新后再保存。")
        values = {**current.model_dump(), **body.values.model_dump()}
        if values["target_url"]:
            token, table, canonical = parse_target_url(values["target_url"])
            values.update(target_app_token=token, target_table_id=table, target_url=canonical)
        elif current.mode == "live":
            raise InputError("请填写要写入的缺陷表链接。")
        if set(body.fields) != set(FIELD_LABELS):
            raise InputError("字段映射不完整，请刷新页面后重试。")
        for key, name in body.fields.items():
            if len(name) > 100 or any(ord(char) < 32 for char in name):
                raise InputError("字段名称过长或包含无效字符。")
            values["field_" + key] = name.strip()
        for key in ("title", "steps", "expected", "actual", "priority", "reporter"):
            if not values["field_" + key]:
                raise InputError(f"请设置「{FIELD_LABELS[key]}」的目标字段。")
        users = list(dict.fromkeys(re.split(r"[,，\s]+", values["allowed_users"].strip())))
        users = [user for user in users if user]
        if any(not re.fullmatch(r"ou_[A-Za-z0-9_-]+", user) for user in users):
            raise InputError("允许用户请填写 ou_ 开头的飞书 open_id，每行一位。")
        if current.mode == "live" and values["bot_enabled"] and not users:
            raise InputError("启用机器人前，请至少添加一位允许用户。")
        values["allowed_users"] = ",".join(users)
        reporter = values["default_reporter"]
        if reporter and not re.fullmatch(r"ou_[A-Za-z0-9_-]+", reporter):
            raise InputError("网页默认提交人请填写 ou_ 开头的飞书 open_id。")
        secret = body.app_secret.get_secret_value() if body.app_secret else ""
        if secret:
            if any(ord(char) < 33 or ord(char) > 126 for char in secret):
                raise InputError("飞书 App Secret 不能包含空格、换行或非 ASCII 字符。")
            values["app_secret"] = SecretStr(secret)
        elif values["app_id"] != current.app_id:
            raise InputError("更换 App ID 时，请同时填写新应用的 App Secret。")
        if values["app_id"] and not re.fullmatch(r"cli_[A-Za-z0-9]+", values["app_id"]):
            raise InputError("请填写 cli_ 开头的飞书 App ID。")
        try:
            return Settings(_env_file=None, **values)
        except ValidationError:
            raise InputError("配置不完整，请检查飞书应用与缺陷表设置。") from None

    def check(self, candidate: Settings) -> dict:
        if candidate.mode == "demo":
            return {"message": "当前为演示模式，未连接飞书。", "field_count": 0}
        client = self.client_factory(candidate)
        try:
            fields = client.verify_fields()
            if candidate.bot_enabled:
                client.bot_open_id()
            return {"message": "应用鉴权、目标表读取和字段映射检查通过。", "field_count": len(fields)}
        finally:
            client.close()

    def save(self, body: ConfigUpdate):
        candidate = self.candidate(body)
        self.check(candidate)  # 慢速鉴权不占配置锁，在途操作可继续读取原快照。
        new_reference = None
        if body.app_secret and body.app_secret.get_secret_value():
            new_reference = self.keychain.put(body.app_secret.get_secret_value())
        with self._lock:
            try:
                with self.store.connection() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    revision, old = self._record(conn)
                    if revision != body.revision:
                        raise Conflict("配置已在另一个页面修改，请刷新后再保存。")
                    reference = new_reference or old.get("secret_ref")
                    keys = (*VALUE_KEYS, "target_app_token", "target_table_id", *("field_" + key for key in FIELD_LABELS))
                    saved = {"overrides": {key: getattr(candidate, key) for key in keys}}
                    if reference:
                        saved["secret_ref"] = reference
                    revision += 1
                    conn.execute("INSERT INTO app_settings VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, body=excluded.body",
                                 (revision, json.dumps(saved, ensure_ascii=False)))
            except BaseException:
                if new_reference:
                    try:
                        self.keychain.delete(new_reference)
                    except Exception:
                        pass
                raise
            self._cached_revision, self._cached_settings = revision, candidate
            # 不删除旧引用：另一个进程可能刚读到旧快照，需让在途请求完成。
            return self.public()


class Services:
    """每次请求捕获一个配置快照，在途操作继续使用原客户端。"""
    def __init__(self, configuration: Configuration, client=None):
        self.configuration = configuration
        self._lock = threading.RLock()
        revision, settings = configuration.snapshot()
        self._revision = revision
        self._service = Service(settings, configuration.store, client if client is not None else configuration.client_factory(settings))
        self._clients = {self._service.client: 0}

    def _retire(self):
        for client, count in list(self._clients.items()):
            if client is not self._service.client and not count:
                client.close()
                del self._clients[client]

    def current(self):
        with self._lock:
            revision, settings = self.configuration.snapshot()
            if revision != self._revision:
                client = self.configuration.client_factory(settings)
                self._service = Service(settings, self.configuration.store, client)
                self._clients[client] = 0
                self._revision = revision
                self._retire()
            return self._service

    @contextmanager
    def acquire(self):
        with self._lock:
            service = self.current()
            self._clients[service.client] += 1
        try:
            yield service
        finally:
            with self._lock:
                self._clients[service.client] -= 1
                self._retire()

    def close(self):
        for client in self._clients:
            client.close()


def load_settings() -> Settings:
    base = Settings()
    return Configuration(base, Store(base.database)).snapshot()[1]
