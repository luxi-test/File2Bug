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


def test_advanced_role_error_explains_subtable_permissions():
    client = client_for(lambda request: httpx.Response(403, json={'code':1254302,'msg':'The role has no permissions.'}))
    with pytest.raises(FeishuError, match='高级权限.*1254302.*可管理.*目标数据表'):
        client.verify_fields()


@pytest.mark.parametrize('code', [99991663, 99991664, 99991665, 99991666, 99991668])
def test_rejected_token_refreshes_once_without_logging_credentials(code, caplog):
    tokens, requests = [], []
    def route(request):
        if request.url.path.endswith('tenant_access_token/internal'):
            token = 'fixture-credential-' + str(len(tokens)+1)
            tokens.append(token)
            return httpx.Response(200, json={'code':0,'tenant_access_token':token,'expire':7200})
        requests.append(request.headers['Authorization'])
        if len(requests) == 1:
            return httpx.Response(401, json={'code':code,'msg':'invalid fixture-credential-1'})
        return httpx.Response(200, json={'code':0,'bot':{'open_id':'ou_bot'}})
    client = FeishuClient(Settings(_env_file=None), httpx.MockTransport(route))
    assert client.bot_open_id() == 'ou_bot'
    assert len(tokens) == 2
    assert requests == ['Bearer '+token for token in tokens]
    assert str(code) in caplog.text
    assert 'fixture-credential' not in caplog.text


def test_repeated_auth_failure_is_bounded():
    attempts = []
    def reject(request):
        attempts.append(request)
        return httpx.Response(401, json={'code':99991663})
    with pytest.raises(FeishuError, match='99991663'):
        client_for(reject).bot_open_id()
    assert len(attempts) == 2


@pytest.mark.parametrize('clock', ['wall', 'monotonic'])
def test_token_cache_expires_when_either_clock_advances(monkeypatch, clock):
    import fail2bug.feishu as module
    now = {'wall':1000., 'monotonic':10.}
    monkeypatch.setattr(module.time, 'time', lambda: now['wall'])
    monkeypatch.setattr(module.time, 'monotonic', lambda: now['monotonic'])
    calls = []
    def route(request):
        calls.append(request)
        return httpx.Response(200, json={'code':0,'tenant_access_token':str(len(calls)),'expire':7200})
    client = FeishuClient(Settings(_env_file=None), httpx.MockTransport(route))
    assert client._access_token() == '1'
    assert client._access_token() == '1'
    # 休眠或时钟调整时，另一计时来源可能没有同步前进。
    now[clock] += 7201
    assert client._access_token() == '2'
    assert len(calls) == 2


def test_concurrent_auth_rejections_share_the_new_token():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    barrier = threading.Barrier(2)
    tokens = []
    def route(request):
        if request.url.path.endswith('tenant_access_token/internal'):
            tokens.append('fixture-' + str(len(tokens)+1))
            return httpx.Response(200, json={'code':0,'tenant_access_token':tokens[-1],'expire':7200})
        if request.headers['Authorization'] == 'Bearer fixture-1':
            barrier.wait(timeout=3)
            return httpx.Response(401, json={'code':99991663})
        return httpx.Response(200, json={'code':0,'bot':{'open_id':'ou_bot'}})
    client = FeishuClient(Settings(_env_file=None), httpx.MockTransport(route))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(client.bot_open_id) for _ in range(2)]
        assert [future.result(timeout=5) for future in futures] == ['ou_bot', 'ou_bot']
    assert len(tokens) == 2


def test_auth_refresh_preserves_write_idempotency_and_payload():
    calls = []
    def route(request):
        calls.append((request.url.params['client_token'], request.read()))
        if len(calls) == 1:
            return httpx.Response(401, json={'code':99991663})
        return httpx.Response(200, json={'code':0,'data':{'record':{'record_id':'rec_ok'}}})
    client = client_for(route)
    assert client.create_record({'title':'example'}, 'stable-operation')['record_id'] == 'rec_ok'
    assert len(calls) == 2 and calls[0] == calls[1]


def test_auth_refresh_does_not_retry_uncertain_writes():
    calls = []
    def route(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401, json={'code':99991663})
        raise httpx.ReadTimeout('network timeout')
    with pytest.raises(SubmissionUnknown):
        client_for(route).create_record({'title':'example'}, 'stable-operation')
    assert len(calls) == 2


def test_auth_refresh_rewinds_uploaded_file(tmp_path):
    path = tmp_path/'evidence.png'
    path.write_bytes(b'fixture-image-content')
    payloads = []
    def route(request):
        payloads.append(request.read())
        if len(payloads) == 1:
            return httpx.Response(401, json={'code':99991663})
        return httpx.Response(200, json={'code':0,'data':{'file_token':'uploaded-file'}})
    assert client_for(route).upload_media(path, 'evidence.png', 'image/png') == 'uploaded-file'
    assert len(payloads) == 2
    assert all(b'fixture-image-content' in body for body in payloads)


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
