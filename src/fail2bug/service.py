from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4
import shutil

from .config import Settings
from .feishu import FeishuClient, FeishuError, SubmissionUnknown
from .models import ReviewUpdate, draft_issues
from .parser import InputError, demo_table, parse_table, title_without_module
from .store import Conflict, Store
from .inference import apply_frequency, infer_item
from .repro import format_repro
from .local_source import open_local, read_workbook
from .online_source import attach_sheet_media
from .titles import apply_title, suggest_title

METADATA = {"problem_type": "问题类型", "module": "模块", "submodule": "子模块", "frequency": "发生概率"}
EDITABLE_METADATA = {*METADATA, "selected", "preconditions", "test_data", "steps", "expected", "actual"}
HINT_KEYS = ("reporter", "device", "environment", "project", *METADATA, "attachments")


class Service:
    def __init__(self, settings: Settings, store: Store, client: FeishuClient):
        self.settings, self.store, self.client = settings, store, client

    def _hints(self, schema, reporter=""):
        hints = {}
        for key in HINT_KEYS:
            field = schema.get(self.settings.fields.get(key, ""), {})
            hints[key] = {"enabled": bool(field), "type": field.get("type", 1), "options": [option["name"] for option in (field.get("property") or {}).get("options", [])]}
        if (reporter or self.settings.default_reporter) == self.settings.default_reporter:
            hints["reporter"]["display_name"] = self.settings.default_reporter_name
        return hints

    def import_sheet(self, url: str, reporter: str = "") -> dict:
        if self.settings.mode != "live":
            raise InputError("当前为演示模式，请使用示例执行表。真实读写需在 .env 中配置飞书应用。")
        rows, title, canonical = self.client.read_sheet(url)
        schema = self.client.verify_fields() or {}
        parsed = parse_table(rows, canonical)
        hints = self._hints(schema, reporter)
        for item in parsed["items"]:
            infer_item(item, hints)
        cached = attach_sheet_media(self.client, self.settings, parsed, rows, canonical, self.attachment_path)
        try:
            batch = self.store.create(parsed, target=self.settings.target_id, mode="live", title=title, reporter=reporter or self.settings.default_reporter, project=self.settings.default_project, environment=self.settings.default_environment, default_hints=hints)
        except BaseException:
            for path in cached:
                path.unlink(missing_ok=True)
            raise
        return self._decorate(batch)

    def import_demo(self) -> dict:
        hints = self._demo_hints()
        batch = self.store.create(parse_table(demo_table(), ""), target="demo", mode="demo", title="智能眼镜 App · 回归执行表", default_hints=hints)
        return self._decorate(batch)

    def _demo_hints(self):
        hints = {key: {"enabled": True, "type": 4 if key in {"device", "environment", "problem_type", "module"} else 3, "options": options} for key, options in {
            "device": ["iPhone 16", "小米15", "Android", "Web"], "environment": ["iOS", "Android", "Web"],
            "problem_type": ["功能异常", "显示异常", "交互缺陷"], "module": ["设备连接", "账号登录", "消息通知", "个人设置"],
            "submodule": ["连接状态", "验证码", "通知设置", "固件升级", "头像"],
            "frequency": ["Very High / Frequent", "High / Likely", "Medium / Occasional", "Low / Rare"],
        }.items()}
        hints["attachments"] = {"enabled": True, "type": 17, "options": []}
        return hints

    def import_file(self, path: Path, reporter: str = "", sheet: str | None = None) -> dict:
        hints = self._hints(self.client.verify_fields() or {}, reporter) if self.settings.mode == "live" else self._demo_hints()
        cached = []
        committed = False
        try:
            with open_local(path, max_bytes=self.settings.max_import_mb * 1024 * 1024) as (book, root):
                parsed, title, files = read_workbook(book, root, sheet=sheet, max_rows=self.settings.max_rows, max_columns=self.settings.max_columns)
                total = 0
                for item in parsed["items"]:
                    infer_item(item, hints)
                    for source in files[item['id']]:
                        size = len(source['data']) if 'data' in source else source['path'].stat().st_size
                        if not size or len(item['attachments']) >= 10 or size > self.settings.max_attachment_mb * 1024 * 1024 or total + size > self.settings.max_import_mb * 1024 * 1024:
                            parsed['warnings'].append(f"{item['case_id']}：附件 {source['name']} 超过数量或大小上限，等待手动补充")
                            continue
                        storage_name = uuid4().hex
                        target = self.attachment_path(storage_name)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        cached.append(target)
                        if 'data' in source:
                            target.write_bytes(source['data'])
                        else:
                            shutil.copyfile(source['path'], target)
                        target.chmod(0o600)
                        item['attachments'].append({'id':str(uuid4()),'name':source['name'],'size':size,'content_type':source['content_type'],'storage_name':storage_name,'linked':False})
                        total += size
                batch = self.store.create(parsed, target=self.settings.target_id, mode=self.settings.mode, title=title, reporter=reporter or self.settings.default_reporter, project=self.settings.default_project, environment=self.settings.default_environment, default_hints=hints)
                committed = True
                return self._decorate(batch)
        finally:
            if not committed:
                for target in cached:
                    target.unlink(missing_ok=True)

    def review(self, batch_id: str) -> dict:
        batch = self.store.get(batch_id)
        expected = [key for key in HINT_KEYS if self.settings.fields.get(key)]
        if batch["mode"] == "live" and batch["target"] == self.settings.target_id and any(key not in batch.get("default_hints", {}) for key in expected):
            hints = self._hints(self.client.verify_fields() or {}, batch["defaults"].get("reporter", ""))
            def upgrade(value):
                if any(item["status"] == "submitting" for item in value["items"]):
                    raise Conflict("本批次正在写入，请完成后再刷新。")
                value["default_hints"] = hints
            batch = self.store.mutate(batch_id, upgrade)
        return self._decorate(batch)

    def _effective(self, item, defaults, key):
        return item.get(key, "")

    def title_suggestion(self, batch_id, item_id, actual):
        item = next((item for item in self.store.get(batch_id)['items'] if item['id'] == item_id), None)
        if not item:
            raise KeyError(item_id)
        if item.get('record_id') or item['status'] in {'unknown', 'submitting'}:
            raise Conflict('已提单或结果待确认的条目保留原标题。')
        return suggest_title(item.get('case_title') or item.get('title', ''), actual, item.get('module', ''))

    def _snapshot(self, item, defaults):
        values = {key: self._effective(item, defaults, key) for key in METADATA if self.settings.fields.get(key)}
        if self.settings.field_attachments:
            values["attachments"] = [entry["id"] for entry in item.get("attachments", [])]
        if self.settings.field_steps:
            values["description"] = format_repro(item)
        return values

    def _baseline(self, item):
        if "metadata_snapshot" in item:
            baseline = dict(item["metadata_snapshot"])
            if self.settings.field_steps and "description" not in baseline:
                baseline["description"] = item.get("write_payload", {}).get(self.settings.field_steps, "")
            return baseline
        # 已提交的旧批次按实际曾写入的字段判断缺项，而非把源模块误当成已写入。
        values = {}
        for key in METADATA:
            name = self.settings.fields.get(key)
            if name:
                raw = item.get("write_payload", {}).get(name, "")
                values[key] = "\n".join(raw) if isinstance(raw, list) else raw
        if self.settings.field_attachments:
            values["attachments"] = [entry["id"] for entry in item.get("attachments", []) if entry.get("linked")]
        if self.settings.field_steps:
            values["description"] = item.get("write_payload", {}).get(self.settings.field_steps, "")
        return values

    def _decorate(self, batch):
        batch["defaults"].pop("problem_type", None)
        batch["defaults"].pop("frequency", None)
        for item in batch["items"]:
            if not item.get("record_id") and item["status"] in {"pending", "failed"}:
                item["title"] = title_without_module(item.get("title", ""), item.get("module", ""))
            for key in METADATA:
                item.setdefault(key, "")
            item.setdefault("attachments", [])
            if not item.get("inference_version") and item["status"] not in {"unknown", "submitting"}:
                infer_item(item, batch.get("default_hints", {}))
            if not item.get("frequency") and item["status"] in {"pending", "failed"}:
                apply_frequency(item, batch.get("default_hints", {}))
            if not item.get('submodule') and item.get('module') and item['status'] not in {'unknown','submitting'}:
                item['submodule']=item['module'].replace('\n','、')
                item.setdefault('inference',{})['submodule']={'value':item['submodule'],'reason':'子模块无法确定，按要求使用模块名','source':'module_fallback'}
            item["formatted_repro"] = format_repro(item)
            item["metadata_baseline"] = self._baseline(item)
            item["metadata_dirty"] = bool(item.get("record_id") and self._snapshot(item, batch["defaults"]) != self._baseline(item))
        return batch

    def update(self, batch_id: str, update: ReviewUpdate) -> dict:
        def apply(batch):
            if batch["revision"] != update.revision:
                raise Conflict("此批次已有更新，请刷新页面后再操作。")
            if any(item["status"] == "submitting" for item in batch["items"]):
                raise Conflict("当前正在提交，请等待结果后再编辑。")
            self._decorate(batch)
            edits = {item.id: item for item in update.items}
            if len(edits) != len(update.items) or set(edits) != {item["id"] for item in batch["items"]}:
                raise InputError("审核条目与原批次不一致，请刷新页面。")
            for item in batch["items"]:
                edit = edits[item["id"]].model_dump(exclude={"id"}, exclude_unset=True)
                previous_actual = item.get('actual', '')
                title_changed = 'title' in edit and edit['title'] != item.get('title', '')
                title_hint = item.get('inference', {}).get('title', {})
                auto_title = title_hint.get('source') in {'actual', 'case_title'} and item.get('title') == title_hint.get('value')
                manual = {key for key in METADATA if key in edit and edit[key] != item.get(key, '')}
                if item["status"] == "unknown":
                    if any(item.get(key, "") != value for key, value in edit.items() if key != "selected"):
                        raise Conflict("结果待确认的条目不能修改，请先重试原操作。")
                elif item.get("record_id"):
                    if any(item.get(key, "") != value for key, value in edit.items() if key not in EDITABLE_METADATA):
                        raise Conflict("已提缺陷仅支持补充分类、复现描述和附件，标题与优先级保持原值。")
                if "module" in edit and edit["module"] != item.get("module") and item.get("inference", {}).get("submodule", {}).get("source") == "module_fallback" and ("submodule" not in edit or edit["submodule"] == item.get("submodule")):
                    edit["submodule"] = edit["module"].replace("\n", "、")
                item.update(edit)
                if not item.get('record_id') and item['status'] != 'unknown':
                    if title_changed:
                        item.setdefault('inference', {})['title'] = {'value':item['title'], 'source':'manual', 'reason':'人工修改', 'needs_review':False}
                    elif item.get('actual', '') != previous_actual and auto_title:
                        apply_title(item)
                for key in manual:
                    item.setdefault('inference', {})[key] = {'value':item.get(key,''),'reason':'人工修改','source':'manual'}
                if item.get('actual','') != previous_actual:
                    for key in ('problem_type','frequency'):
                        if item.get('inference',{}).get(key,{}).get('source') != 'manual':
                            item[key] = ''
                    infer_item(item, batch.get('default_hints', {}))
                item["issues"] = draft_issues(item)
                if not item["case_id"]:
                    item["issues"].append("源表缺少用例ID，请先补齐源表后重新导入")
            batch["defaults"].update(update.defaults.model_dump(exclude_unset=True))
            self._decorate(batch)
        return self.store.mutate(batch_id, apply)

    def _fields(self, item: dict, defaults: dict, schema: dict | None = None, source_url: str = "", metadata_only=False) -> dict:
        values = {**item, **defaults, "status": self.settings.initial_status, "source": source_url}
        values.update({key: self._effective(item, defaults, key) for key in METADATA})
        values["attachments"] = [{"file_token": entry["file_token"]} for entry in item.get("attachments", []) if entry.get("file_token")]
        labels = {"steps": "复现步骤", "expected": "预期结果", "actual": "实际结果", "version": "测试版本", "case_id": "关联用例", "evidence": "证据原文", "source": "来源链接", "reporter": "提交人"}
        groups = {}
        for key, name in self.settings.fields.items():
            if not metadata_only or key in {*METADATA, "attachments", "steps", "expected", "actual"}:
                groups.setdefault(name, []).append((key, values.get(key, "")))
        payload = {}
        for name, entries in groups.items():
            text = entries[0][1] if len(entries) == 1 else "\n\n".join(f"【{labels.get(key, key)}】\n{value}" for key, value in entries if value)
            note_keys = ("version", "case_id", "source")
            if set(note_keys).issubset(key for key, _ in entries):
                note_values = dict(entries)
                written_on = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
                text = f"{written_on} QA:\n" + "\n".join(f"【{labels[key]}】\n{note_values[key]}" for key in note_keys)
            if name == self.settings.field_steps:
                text = format_repro(item)
            field = (schema or {}).get(name, {})
            kind = field.get("type", 17 if entries[0][0] == "attachments" else 1)
            if kind == 17:
                payload[name] = text
            elif kind == 11:
                if not str(text).startswith("ou_"):
                    raise InputError(f"「{name}」是人员字段，请填写飞书用户 open_id（ou_ 开头），姓名可另记在备注中。")
                payload[name] = [{"id": text}]
            elif kind == 4:
                choices = list(dict.fromkeys(value.strip() for value in str(text).splitlines() if value.strip()))
                allowed = {option["name"] for option in (field.get("property") or {}).get("options", [])}
                # 飞书会在写记录时把新的模块值加入这个多选字段。
                if entries[0][0] != "module" and any(choice not in allowed for choice in choices):
                    raise InputError(f"「{name}」请使用缺陷表已有选项。")
                payload[name] = choices
            elif kind == 3:
                allowed = {option["name"] for option in (field.get("property") or {}).get("options", [])}
                project = len(entries) == 1 and entries[0][0] == "project"
                submodule_fallback = name == self.settings.field_submodule and text == item.get("module", "").replace("\n", "、")
                if text and text not in allowed and not (project or submodule_fallback):
                    raise InputError(f"「{name}」没有选项「{text}」，请使用缺陷表已有选项。")
                payload[name] = text
            else:
                payload[name] = text
        return payload

    def attachment_path(self, storage_name):
        if not storage_name or Path(storage_name).name != storage_name:
            raise InputError("附件存储标识无效。")
        return self.settings.database.parent / "uploads" / storage_name

    def add_attachment(self, batch_id, item_id, attachment, revision):
        if not 0 < attachment.get('size',0) <= self.settings.max_attachment_mb * 1024 * 1024:
            raise InputError('附件为空或超过大小上限。')
        def add(batch):
            if batch["revision"] != revision:
                raise Conflict("此批次已有更新，请刷新后上传。")
            if any(item["status"] == "submitting" for item in batch["items"]):
                raise Conflict("本批次正在提交，暂不能修改附件。")
            item = next((item for item in batch["items"] if item["id"] == item_id), None)
            if not item:
                raise KeyError(item_id)
            if item["status"] == "unknown":
                raise Conflict("请先确认上次提交结果，再修改附件。")
            if len(item.get("attachments", [])) >= 10:
                raise InputError("每条缺陷最多选择 10 个附件。")
            item.setdefault("attachments", []).append(attachment)
            self._decorate(batch)
        return self.store.mutate(batch_id, add)

    def remove_attachment(self, batch_id, item_id, attachment_id, revision):
        removed = []
        def remove(batch):
            if batch["revision"] != revision:
                raise Conflict("此批次已有更新，请刷新后操作。")
            if any(item["status"] == "submitting" for item in batch["items"]):
                raise Conflict("本批次正在提交，暂不能修改附件。")
            item = next((item for item in batch["items"] if item["id"] == item_id), None)
            if not item:
                raise KeyError(item_id)
            if item["status"] == "unknown":
                raise Conflict("请先重试原提交，再修改附件。")
            entry = next((entry for entry in item.get("attachments", []) if entry["id"] == attachment_id), None)
            if not entry:
                raise KeyError(attachment_id)
            if entry.get("linked"):
                raise Conflict("已写入缺陷的附件请在飞书中管理。")
            removed.append(entry)
            item["attachments"].remove(entry)
            self._decorate(batch)
        result = self.store.mutate(batch_id, remove)
        self.attachment_path(removed[0]["storage_name"]).unlink(missing_ok=True)
        return result

    def submit(self, batch_id: str, revision: int) -> dict:
        batch = self.store.get(batch_id)
        schema = {}
        if batch["mode"] == "live":
            if self.settings.mode != "live" or batch["target"] != self.settings.target_id:
                raise Conflict("当前缺陷表配置与创建批次时不同，已停止提交。请恢复原配置或重新导入。")
            schema = self.client.verify_fields() or {}
        work = []

        def prepare(value):
            if value["revision"] != revision:
                raise Conflict("此批次已有更新，请刷新页面后再提交。")
            if any(item["status"] == "submitting" for item in value["items"]):
                raise Conflict("此批次正在提交，请勿重复点击。")
            self._decorate(value)
            selected = [item for item in value["items"] if item["selected"] and (item["status"] != "succeeded" or item["metadata_dirty"])]
            if not selected:
                raise InputError("请先勾选需要写入的缺陷。已成功且无修改的条目不会再次提交。")
            if any(not item.get("record_id") and item["status"] != "unknown" for item in selected):
                labels = {"reporter": "提交人", "version": "版本", "device": "测试设备", "environment": "环境"}
                missing = [label for key, label in labels.items() if not value["defaults"][key].strip()]
                if missing:
                    raise InputError("请补齐批次信息：" + "、".join(missing))
            for item in selected:
                retry = item["status"] == "unknown"
                if not retry:
                    if not item.get("record_id"):
                        issues = draft_issues(item)
                        if not item["case_id"]:
                            issues.append("源表缺少用例ID")
                        if issues:
                            raise InputError(f"{item['case_id'] or '第' + str(item['row_no']) + '行'}：" + "；".join(issues))
                        if self.settings.field_project and not value["defaults"].get("project") and value["mode"] == "live":
                            raise InputError("请补充所属项目。")
                    for key, label in METADATA.items():
                        if self.settings.fields.get(key) and not self._effective(item, value["defaults"], key).strip():
                            raise InputError(f"{item['case_id']}：请填写{label}。")
                    operation = "update" if item.get("record_id") else "create"
                    payload = self._fields(item, value["defaults"], schema, value.get("source_url") or value.get('source_label',''), metadata_only=operation == "update")
                    if payload != item.get("write_payload") or operation != item.get("write_operation"):
                        item["operation_id"] = str(uuid4())
                    item.update(write_payload=payload, write_operation=operation)
                    item["pending_metadata_snapshot"] = self._snapshot(item, value["defaults"])
                item.update(status="submitting", error="")
                work.append({"id": item["id"], "retry": retry, "snapshot": item.get("pending_metadata_snapshot", self._snapshot(item, value["defaults"]))})

        self.store.mutate(batch_id, prepare)
        for operation in work:
            try:
                current = self.store.get(batch_id)
                item = next(item for item in current["items"] if item["id"] == operation["id"])
                if not operation["retry"]:
                    for entry in item.get("attachments", []):
                        if not entry.get("file_token"):
                            token = "demo-" + entry["id"] if batch["mode"] == "demo" else self.client.upload_media(self.attachment_path(entry["storage_name"]), entry["name"], entry["content_type"])
                            entry["file_token"] = token
                            def cache(value):
                                target = next(row for row in value["items"] if row["id"] == item["id"])
                                next(file for file in target["attachments"] if file["id"] == entry["id"])["file_token"] = token
                            self.store.mutate(batch_id, cache)
                    if self.settings.field_attachments:
                        tokens = {entry["file_token"]: {"file_token": entry["file_token"]} for entry in item.get("attachments", [])}
                        if item.get("record_id") and batch["mode"] == "live":
                            remote = self.client.get_record(item["record_id"])
                            for existing in remote.get("fields", {}).get(self.settings.field_attachments, []) or []:
                                tokens.setdefault(existing["file_token"], {"file_token": existing["file_token"]})
                        item["write_payload"][self.settings.field_attachments] = list(tokens.values())
                        def save_payload(value):
                            next(row for row in value["items"] if row["id"] == item["id"])["write_payload"] = item["write_payload"]
                        self.store.mutate(batch_id, save_payload)
                if batch["mode"] == "demo":
                    result = {"record_id": item.get("record_id") or "DEMO-" + item["id"][:8], "record_url": ""}
                elif item.get("write_operation") == "update":
                    result = self.client.update_record(item["record_id"], item["write_payload"], item["operation_id"])
                else:
                    result = self.client.create_record(item["write_payload"], item["operation_id"])
                outcome = {"status": "succeeded", **result, "error": "", "metadata_snapshot": operation["snapshot"], "metadata_dirty": False}
            except SubmissionUnknown as exc:
                outcome = {"status": "unknown", "error": str(exc)}
            except (FeishuError, InputError) as exc:
                outcome = {"status": "failed", "error": str(exc)}
            except Exception:
                outcome = {"status": "unknown", "error": "写入出现异常，结果待确认；请重试原操作。"}
            def finish(value):
                item = next(item for item in value["items"] if item["id"] == operation["id"])
                item.update(outcome)
                if outcome["status"] == "succeeded":
                    for entry in item.get("attachments", []):
                        entry["linked"] = True
            self.store.mutate(batch_id, finish)
        return self._decorate(self.store.get(batch_id))
