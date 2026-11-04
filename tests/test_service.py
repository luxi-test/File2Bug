from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from fail2bug.config import Settings
from fail2bug.feishu import FeishuError, SubmissionUnknown
from fail2bug.models import ReviewUpdate
from fail2bug.parser import InputError, demo_table, parse_table
from fail2bug.service import Service
from fail2bug.store import Conflict, Store


class Client:
    def __init__(self):
        self.calls = []
        self.failures = []

    def verify_fields(self):
        pass

    def create_record(self, fields, operation_id):
        self.calls.append((fields, operation_id))
        if self.failures:
            raise self.failures.pop(0)
        return {"record_id": "rec" + str(len(self.calls)), "record_url": "https://feishu.cn/base/test"}


@pytest.fixture
def service(tmp_path):
    settings = Settings(_env_file=None, mode="live", database=tmp_path / "db.sqlite", app_id="cli_test", app_secret="secret", access_key="password", target_app_token="app", target_table_id="tbl")
    return Service(settings, Store(settings.database), Client())


def new_batch(service):
    return service.store.create(parse_table(demo_table(), ""), target=service.settings.target_id, mode="live", title="测试")


def ready(service, batch, selected=1):
    fields = ("id", "selected", "title", "priority", "steps", "expected", "actual", "evidence")
    edits = []
    for index, item in enumerate(batch["items"]):
        edit = {key: item[key] for key in fields}
        edit["selected"] = index < selected
        edit["actual"] = edit["actual"] or "人工补充的实际表现"
        edits.append(edit)
    return service.update(batch["id"], ReviewUpdate(revision=batch["revision"], defaults={"reporter": "测试同学", "version": "1.0", "device": "iPhone", "environment": "预发"}, items=edits))


def test_repeated_submit_skips_successful_records(service):
    batch = ready(service, new_batch(service), selected=2)
    result = service.submit(batch["id"], batch["revision"])
    assert len(service.client.calls) == 2
    assert sum(item["status"] == "succeeded" for item in result["items"]) == 2
    with pytest.raises(InputError, match="已成功"):
        service.submit(batch["id"], result["revision"])
    assert len(service.client.calls) == 2


def test_incomplete_selected_item_blocks_all_writes(service):
    batch = new_batch(service)
    def defaults(value):
        value["defaults"] = {"reporter": "A", "version": "1", "device": "D", "environment": "E"}
    batch = service.store.mutate(batch["id"], defaults)
    with pytest.raises(InputError, match="实际结果"):
        service.submit(batch["id"], batch["revision"])
    assert service.client.calls == []
    assert all(item["status"] == "pending" for item in service.store.get(batch["id"])["items"])


def test_unknown_retry_uses_identical_operation_and_payload(service):
    batch = ready(service, new_batch(service))
    service.client.failures = [SubmissionUnknown("响应丢失")]
    result = service.submit(batch["id"], batch["revision"])
    assert result["items"][0]["status"] == "unknown"
    # 全局批次信息变化不应改变结果待确认条目的原始提交内容。
    result = service.store.mutate(batch["id"], lambda value: value["defaults"].update(version="2.0"))
    final = service.submit(batch["id"], result["revision"])
    assert service.client.calls[0] == service.client.calls[1]
    assert final["items"][0]["status"] == "succeeded"


def test_partial_failure_retries_failed_item_only(service):
    batch = ready(service, new_batch(service), selected=2)
    service.client.failures = [FeishuError("字段类型错误")]
    result = service.submit(batch["id"], batch["revision"])
    assert [item["status"] for item in result["items"][:2]] == ["failed", "succeeded"]
    final = service.submit(batch["id"], result["revision"])
    assert len(service.client.calls) == 3
    assert final["items"][0]["status"] == "succeeded"


def test_review_revision_and_target_binding(service):
    batch = ready(service, new_batch(service))
    with pytest.raises(Conflict, match="已有更新"):
        service.submit(batch["id"], batch["revision"] - 1)
    service.settings.target_table_id = "other"
    with pytest.raises(Conflict, match="缺陷表配置"):
        service.submit(batch["id"], batch["revision"])
    assert not service.client.calls


def test_concurrent_clicks_only_one_write(service):
    batch = ready(service, new_batch(service))
    started, release = threading.Event(), threading.Event()
    original = service.client.create_record
    def blocking_create(fields, operation_id):
        started.set()
        assert release.wait(5)
        return original(fields, operation_id)
    service.client.create_record = blocking_create
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.submit, batch["id"], batch["revision"])
        assert started.wait(5)
        with pytest.raises(Conflict):
            service.submit(batch["id"], batch["revision"])
        release.set()
        first.result(timeout=5)
    assert len(service.client.calls) == 1


def test_restart_preserves_operation_for_retry(service):
    batch = ready(service, new_batch(service))
    def interrupted(value):
        item = value["items"][0]
        item.update(status="submitting", operation_id="existing-operation", write_payload={"缺陷标题": "原始标题"})
    service.store.mutate(batch["id"], interrupted)
    service.store.recover()
    result = service.store.get(batch["id"])
    assert result["items"][0]["status"] == "unknown"
    service.submit(batch["id"], result["revision"])
    assert service.client.calls == [({"缺陷标题": "原始标题"}, "existing-operation")]


def test_existing_table_field_types_and_combined_description(service):
    settings = service.settings
    settings.field_steps = settings.field_expected = settings.field_actual = "重现步骤"
    settings.field_version = settings.field_case_id = settings.field_evidence = settings.field_source = "备注"
    settings.field_reporter = "提交人(QA)"
    settings.field_device = "设备"
    settings.field_environment = "系统"
    settings.field_project = "所属项目"
    schema = {
        "提交人(QA)": {"type": 11},
        "设备": {"type": 4, "property": {"options": [{"name": "测试手机"}]}},
        "系统": {"type": 4, "property": {"options": [{"name": "Android"}]}},
        "所属项目": {"type": 3, "property": {"options": [{"name": "测试项目"}]}},
    }
    item = new_batch(service)["items"][0]
    defaults = {"reporter": "ou_testuser", "device": "测试手机", "environment": "Android", "project": "测试项目", "version": "1.0"}
    fields = service._fields(item, defaults, schema, "https://team.feishu.cn/sheets/test")
    assert fields["提交人(QA)"] == [{"id": "ou_testuser"}]
    assert fields["设备"] == ["测试手机"]
    assert fields["系统"] == ["Android"]
    assert fields["所属项目"] == "测试项目"
    assert all(value in fields["重现步骤"] for value in [item["steps"], item["expected"], item["actual"]])
    assert all(value in fields["备注"] for value in [item["case_id"], "1.0", "https://team.feishu.cn/sheets/test"])


def test_person_name_is_not_silently_written_as_person_id(service):
    item = new_batch(service)["items"][0]
    with pytest.raises(InputError, match="open_id"):
        service._fields(item, {"reporter": "测试同学"}, {"提交人": {"type": 11}})


def test_multi_select_does_not_create_unreviewed_options(service):
    item = new_batch(service)["items"][0]
    with pytest.raises(InputError, match="已有选项"):
        service._fields(item, {"device": "不存在的设备"}, {"测试设备": {"type": 4, "property": {"options": []}}})


@pytest.fixture
def metadata_service(service):
    s = service.settings
    s.field_module = '模块'
    s.field_submodule = '子模块'
    s.field_problem_type = '问题类型'
    s.field_frequency = '发生概率'
    s.field_attachments = '截图或视频'
    service.client.updates = []
    service.client.uploads = []
    schema = {
        '模块': {'type': 4, 'property': {'options': [{'name': '设备连接'}, {'name': '账号登录'}, {'name': '消息通知'}, {'name': '个人设置'}]}},
        '子模块': {'type': 3, 'property': {'options': [{'name': '连接状态'}]}},
        '问题类型': {'type': 4, 'property': {'options': [{'name': '功能异常'}, {'name': '显示异常'}]}},
        '发生概率': {'type': 3, 'property': {'options': [{'name': 'High / Likely'}, {'name': 'Low / Rare'}]}},
        '截图或视频': {'type': 17},
    }
    service.client.verify_fields = lambda: schema
    service.client.get_record = lambda record_id: {'record_id': record_id, 'fields': {'截图或视频': [{'file_token': 'remote-existing'}]}}
    def update(record_id, fields, operation_id):
        service.client.updates.append((record_id, fields, operation_id))
        if service.client.failures:
            raise service.client.failures.pop(0)
        return {'record_id': record_id}
    service.client.update_record = update
    def upload(path, name, content_type):
        assert path.is_file()
        service.client.uploads.append(name)
        return 'uploaded-token'
    service.client.upload_media = upload
    return service


def ready_metadata(service):
    batch = ready(service, new_batch(service))
    def enrich(value):
        value['items'][0].update(module='设备连接', submodule='连接状态', problem_type='功能异常', frequency='High / Likely')
    return service.store.mutate(batch['id'], enrich)


def test_metadata_fields_use_real_target_types(metadata_service):
    service = metadata_service
    batch = ready_metadata(service)
    result = service.submit(batch['id'], batch['revision'])
    fields = service.client.calls[0][0]
    assert fields['模块'] == ['设备连接']
    assert fields['问题类型'] == ['功能异常']
    assert fields['子模块'] == '连接状态'
    assert fields['发生概率'] == 'High / Likely'
    assert result['items'][0]['metadata_dirty'] is False
    with pytest.raises(InputError, match='已成功'):
        service.submit(batch['id'], result['revision'])
    assert len(service.client.calls) == 1


def test_existing_draft_can_submit_source_module_and_default_frequency(metadata_service):
    service = metadata_service
    schema = service.client.verify_fields()
    schema['发生概率']['property']['options'].append({'name': 'Very High / Frequent'})
    batch = ready_metadata(service)

    def legacy(value):
        value['default_hints'] = service._hints(schema)
        value['items'][0].update(module='示例新模块', submodule='示例新模块', frequency='')

    batch = service.store.mutate(batch['id'], legacy)
    assert service.review(batch['id'])['items'][0]['frequency'] == 'Very High / Frequent'
    result = service.submit(batch['id'], batch['revision'])
    fields = service.client.calls[0][0]
    assert fields['模块'] == ['示例新模块']
    assert fields['发生概率'] == 'Very High / Frequent'
    assert result['items'][0]['status'] == 'succeeded'


def test_existing_bug_backfill_updates_only_requested_fields(metadata_service):
    service = metadata_service
    batch = ready_metadata(service)
    def legacy(value):
        value['items'][0].update(status='succeeded', record_id='rec_existing', record_url='https://test/record', write_payload={'缺陷标题':'原标题'})
    batch = service.store.mutate(batch['id'], legacy)
    result = service.submit(batch['id'], batch['revision'])
    assert not service.client.calls
    record_id, fields, _ = service.client.updates[0]
    assert record_id == 'rec_existing'
    assert set(fields) == {'模块','子模块','问题类型','发生概率','截图或视频','复现步骤','预期结果','实际结果'}
    assert fields['截图或视频'] == [{'file_token': 'remote-existing'}]
    assert result['items'][0]['record_id'] == 'rec_existing'
    assert not result['items'][0]['metadata_dirty']


def test_required_classification_blocks_before_any_write(metadata_service):
    service = metadata_service
    batch = ready_metadata(service)
    batch = service.store.mutate(batch['id'], lambda value: value['items'][0].update(frequency=''))
    with pytest.raises(InputError, match='发生概率'):
        service.submit(batch['id'], batch['revision'])
    assert not service.client.calls and not service.client.updates


def test_attachment_upload_token_cached_and_foreign_attachments_preserved(metadata_service):
    service = metadata_service
    batch = ready_metadata(service)
    item = batch['items'][0]
    path = service.attachment_path('cached-file')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'image fixture')
    attachment = {'id':'file1','name':'证据.png','size':13,'content_type':'image/png','storage_name':'cached-file','linked':False}
    batch = service.add_attachment(batch['id'], item['id'], attachment, batch['revision'])
    def legacy(value):
        value['items'][0].update(status='succeeded',record_id='rec_existing',write_payload={})
    batch = service.store.mutate(batch['id'], legacy)
    service.client.failures = [SubmissionUnknown('响应丢失')]
    first = service.submit(batch['id'], batch['revision'])
    assert first['items'][0]['status'] == 'unknown'
    final = service.submit(batch['id'], first['revision'])
    assert service.client.uploads == ['证据.png']
    assert service.client.updates[0] == service.client.updates[1]
    assert {entry['file_token'] for entry in service.client.updates[0][1]['截图或视频']} == {'remote-existing','uploaded-token'}
    assert final['items'][0]['attachments'][0]['linked']
    assert not service.client.calls
