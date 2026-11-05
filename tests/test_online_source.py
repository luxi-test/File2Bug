import httpx
import pytest

from fail2bug.config import Settings
from fail2bug.feishu import FeishuClient, FeishuError
from fail2bug.service import Service
from fail2bug.store import Store


def test_online_images_and_video_follow_case_rows_through_submission(tmp_path):
    settings = Settings(
        _env_file=None, mode="live", database=tmp_path / "state.db",
        app_id="fixture", app_secret="fixture", access_key="fixture",
        target_app_token="fixture", target_table_id="table", field_attachments="附件",
    )
    rendered = [
        ["用例ID", "PRD模块", "标题", "优先级", "操作步骤", "预期结果", "执行结果", "实际结果", "证据"],
        ["ONLINE-1", "相册", "拍照失败", "P1", "拍照", "保存", "fail", "没有保存", ""],
        ["PASS-1", "相册", "通过项", "P2", "查看", "显示", "pass", "", ""],
        ["ONLINE-2", "相册", "图片异常", "P2", "查看", "显示", "fail", "无法显示", ""],
    ]
    image = {"type": "embed-image", "fileToken": "image-token", "text": "截图.png"}
    video = {"type": "attachment", "fileToken": "video-token", "text": "录屏.mp4"}
    rich = [rendered[0], [*rendered[1][:-1], [image, image, video]],
            [*rendered[2][:-1], {"fileToken": "pass-only-token"}],
            [*rendered[3][:-1], image]]
    downloaded, uploaded, records = [], [], []

    def handler(request):
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"tenant_access_token": "fixture", "expire": 7200})
        if path.endswith("/sheets/query"):
            data = {"sheets": [{"sheet_id": "tab", "title": "执行表",
                               "grid_properties": {"row_count": 4, "column_count": 9}}]}
        elif "/values/" in path:
            assert "A1:I4" in path
            values = rich if request.url.params["valueRenderOption"] == "Formula" else rendered
            data = {"valueRange": {"values": values}}
        elif path.endswith("/float_images/query"):
            data = {"items": [{"range": "tab!I2:I2", "float_image_token": "image-token"},
                              {"range": "tab!I3:I3", "float_image_token": "pass-only-token"}]}
        elif path.endswith("/fields"):
            data = {"items": [{"field_name": name, "type": 17 if logical == "attachments" else 1}
                              for logical, name in settings.fields.items()]}
        elif path.endswith("/download"):
            token = path.split("/")[-2]
            downloaded.append(token)
            assert token != "pass-only-token"
            mime = "video/mp4" if token == "video-token" else "image/png"
            return httpx.Response(200, headers={"content-type": mime}, content=b"fixture-media")
        elif path.endswith("/upload_all"):
            content = request.read()
            assert b"bitable_file" in content and b"fixture-media" in content
            uploaded.append(content)
            data = {"file_token": "uploaded-" + str(len(uploaded))}
        elif path.endswith("/records"):
            import json
            records.append(json.loads(request.content))
            data = {"record": {"record_id": "record-" + str(len(records))}}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"code": 0, "data": data})

    client = FeishuClient(settings, transport=httpx.MockTransport(handler))
    service = Service(settings, Store(settings.database), client)
    try:
        batch = service.import_sheet("https://team.feishu.cn/sheets/book?sheet=tab")
        assert downloaded == ["image-token", "video-token"]
        first, second = batch["items"]
        assert len(first["attachments"]) == 2 and len(second["attachments"]) == 1
        assert first["attachments"][1]["content_type"] == "video/mp4"
        assert first["attachments"][0]["storage_name"] != second["attachments"][0]["storage_name"]
        assert not batch["warnings"]
        batch = service.store.mutate(batch["id"], lambda b: b["defaults"].update(
            reporter="QA", device="设备", version="1.0", environment="测试"))
        result = service.submit(batch["id"], batch["revision"])
        assert all(item["status"] == "succeeded" for item in result["items"])
        assert len(uploaded) == 3
        assert len(records[0]["fields"]["附件"]) == 2
        assert len(records[1]["fields"]["附件"]) == 1
    finally:
        client.close()


@pytest.mark.parametrize("response,limit", [
    (httpx.Response(403, json={"code": 91403}), 100),
    (httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"too-large"), 3),
    (httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=b"<svg/>"), 100),
])
def test_failed_online_media_download_leaves_no_partial_file(tmp_path, response, limit):
    client = FeishuClient(Settings(_env_file=None), transport=httpx.MockTransport(lambda _: response))
    client._token, client._expires = "fixture", float("inf")
    destination = tmp_path / "partial"
    try:
        with pytest.raises(FeishuError):
            client.download_media("media-token", destination, "录屏.mp4", limit)
        assert not destination.exists()
    finally:
        client.close()
