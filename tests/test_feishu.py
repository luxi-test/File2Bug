import httpx
import pytest

from fail2bug.config import Settings
from fail2bug.feishu import FeishuClient, FeishuError, SubmissionUnknown


def client_for(handler):
    settings = Settings(_env_file=None, app_id="id", app_secret="secret", target_app_token="app", target_table_id="tbl")
    def route(request):
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "test_token", "expire": 7200})
        return handler(request)
    return FeishuClient(settings, httpx.MockTransport(route))


def test_business_error_in_http_200_is_failure():
    client = client_for(lambda request: httpx.Response(200, json={"code": 1254060, "msg": "invalid"}))
    with pytest.raises(FeishuError, match="1254060"):
        client.create_record({"title": "test"}, "stable-token")


def test_missing_scope_explains_application_permission_and_publication():
    client = client_for(lambda request: httpx.Response(400, json={"code": 99991672, "msg": "Access denied. One of the following scopes is required: [bitable:app, base:field:read]"}))
    with pytest.raises(FeishuError, match="应用身份权限.*发布应用版本.*base:field:read"):
        client.verify_fields()


def test_write_timeout_is_unknown():
    def handler(request):
        assert request.url.params["client_token"] == "stable-token"
        raise httpx.ReadTimeout("timeout")
    with pytest.raises(SubmissionUnknown):
        client_for(handler).create_record({"title": "test"}, "stable-token")


def test_server_business_error_also_preserves_unknown_outcome():
    client = client_for(lambda request: httpx.Response(200, json={"code": 1255001, "msg": "InternalError"}))
    with pytest.raises(SubmissionUnknown):
        client.create_record({"title": "test"}, "stable-token")


def test_bot_info_supports_top_level_response():
    client = client_for(lambda request: httpx.Response(200, json={"code": 0, "bot": {"open_id": "ou_bot"}}))
    assert client.bot_open_id() == "ou_bot"


def test_sheet_reads_preserve_empty_gaps():
    def handler(request):
        if request.url.path.endswith("sheets/query"):
            return httpx.Response(200, json={"code": 0, "data": {"sheets": [{"sheet_id": "s1", "title": "执行表", "grid_properties": {"row_count": 502, "column_count": 2}}]}})
        range_value = request.url.path.split("/values/")[1]
        values = [["first"]] if "A1:" in range_value else [["last"]]
        return httpx.Response(200, json={"code": 0, "data": {"valueRange": {"values": values}}})
    rows, title, canonical = client_for(handler).read_sheet("https://team.feishu.cn/sheets/abc")
    assert len(rows) == 502
    assert rows[0] == ["first"]
    assert rows[499] == []
    assert rows[500] == ["last"]
    assert canonical.endswith("?sheet=s1")


def test_media_upload_uses_bitable_parent_and_returns_token(tmp_path):
    path=tmp_path/'image.png';path.write_bytes(b'PNG fixture')
    def handler(request):
        assert request.url.path.endswith('/medias/upload_all')
        body=request.read()
        assert b'bitable_file' in body and b'PNG fixture' in body
        return httpx.Response(200,json={'code':0,'data':{'file_token':'uploaded-file'}})
    assert client_for(handler).upload_media(path,'证据.png','image/png') == 'uploaded-file'


def test_video_uses_chunked_media_upload(tmp_path):
    path=tmp_path/'video.mp4'
    with path.open('wb') as handle:handle.truncate(21*1024*1024)
    calls=[]
    def handler(request):
        endpoint=request.url.path.rsplit('/',1)[-1];calls.append(endpoint)
        if endpoint=='upload_prepare':
            return httpx.Response(200,json={'code':0,'data':{'upload_id':'upload','block_size':10*1024*1024,'block_num':3}})
        if endpoint=='upload_part':
            assert b'upload' in request.read()
            return httpx.Response(200,json={'code':0})
        return httpx.Response(200,json={'code':0,'data':{'file_token':'video-token'}})
    assert client_for(handler).upload_media(path,'录屏.mp4','video/mp4') == 'video-token'
    assert calls==['upload_prepare','upload_part','upload_part','upload_part','upload_finish']
