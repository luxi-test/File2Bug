from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="F2B_", env_file=".env", extra="ignore")

    mode: Literal["demo", "live"] = "demo"
    database: Path = Path("data/fail2bug.db")
    base_url: str = "http://127.0.0.1:8000"
    host: str = "127.0.0.1"
    app_id: str = ""
    app_secret: SecretStr = SecretStr("")
    target_app_token: str = ""
    target_table_id: str = ""
    target_url: str = ""
    source_url: str = ""
    access_key: SecretStr = SecretStr("")
    allowed_users: str = ""
    max_rows: int = 10000
    max_columns: int = 100
    max_attachment_mb: int = 200
    max_import_mb: int = 200

    field_title: str = "缺陷标题"
    field_steps: str = "复现步骤"
    field_expected: str = "预期结果"
    field_actual: str = "实际结果"
    field_priority: str = "优先级"
    field_reporter: str = "提交人"
    field_version: str = "版本"
    field_device: str = "测试设备"
    field_environment: str = "环境"
    field_case_id: str = "关联用例"
    field_evidence: str = ""
    field_status: str = ""
    field_project: str = ""
    field_source: str = ""
    field_problem_type: str = ""
    field_module: str = ""
    field_submodule: str = ""
    field_frequency: str = ""
    field_attachments: str = ""
    initial_status: str = "Open(QA)"
    default_project: str = ""
    default_reporter: str = ""
    default_reporter_name: str = ""
    default_environment: str = ""

    @model_validator(mode="after")
    def validate_live(self):
        if self.mode == "live":
            required = ("app_id", "target_app_token", "target_table_id")
            missing = [key for key in required if not getattr(self, key).strip()]
            for key in ("app_secret", "access_key"):
                if not getattr(self, key).get_secret_value().strip():
                    missing.append(key)
            if missing:
                raise ValueError("live 模式缺少配置：" + ", ".join(missing))
        return self

    @property
    def target_id(self) -> str:
        if self.mode == "demo":
            return "demo"
        return f"{self.target_app_token}/{self.target_table_id}"

    @property
    def fields(self) -> dict[str, str]:
        keys = ("title", "steps", "expected", "actual", "priority", "reporter", "version", "device", "environment", "case_id", "evidence", "status", "project", "source", "problem_type", "module", "submodule", "frequency", "attachments")
        return {key: getattr(self, f"field_{key}") for key in keys if getattr(self, f"field_{key}")}
