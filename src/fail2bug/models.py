from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    case_id: str
    row_no: int
    title: str
    module: str = ""
    source_feature: str = ""
    preconditions: str = ""
    test_data: str = ""
    frequency_evidence: str = ""
    reproductions: str = ""
    attempts: str = ""
    problem_type: str = ""
    submodule: str = ""
    frequency: str = ""
    attachments: list[dict] = Field(default_factory=list)
    priority: str = "P2"
    steps: str = ""
    expected: str = ""
    actual: str = ""
    evidence: str = ""
    selected: bool = True
    status: Literal["pending", "submitting", "succeeded", "failed", "unknown"] = "pending"
    record_id: str = ""
    record_url: str = ""
    error: str = ""
    issues: list[str] = Field(default_factory=list)


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reporter: str = ""
    version: str = ""
    device: str = ""
    environment: str = ""
    project: str = ""


class DraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str
    selected: bool
    title: str = Field(max_length=300)
    priority: Literal["P0", "P1", "P2", "P3"]
    steps: str = Field(max_length=20000)
    expected: str = Field(max_length=20000)
    actual: str = Field(max_length=20000)
    preconditions: str = Field(default="", max_length=20000)
    test_data: str = Field(default="", max_length=20000)
    evidence: str = Field(max_length=10000)
    module: str = Field(default="", max_length=3000)
    problem_type: str = Field(default="", max_length=3000)
    submodule: str = Field(default="", max_length=1000)
    frequency: str = Field(default="", max_length=1000)


class ReviewUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    defaults: Defaults
    items: list[DraftEdit] = Field(max_length=10000)


def draft_issues(item: dict) -> list[str]:
    fields = {"title": "缺陷标题", "steps": "复现步骤", "expected": "预期结果", "actual": "实际结果"}
    issues = [f"请补充{label}" for key, label in fields.items() if not str(item.get(key, "")).strip()]
    if item.get("priority") not in {"P0", "P1", "P2", "P3"}:
        issues.append("请选择有效优先级")
    return issues
