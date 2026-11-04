from fastapi.testclient import TestClient

from fail2bug.app import create_app
from fail2bug.config import Settings


def test_demo_pages_and_api(tmp_path):
    app = create_app(Settings(_env_file=None, database=tmp_path / "db.sqlite"))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        batch = client.post("/api/batches/demo").json()
        response = client.get(f"/batches/{batch['id']}")
        assert response.status_code == 200
        assert "缺陷清单" in response.text
        assert len(batch["items"]) == 5
        assert "target" not in batch
        assert client.post("/api/batches", json={"url": "https://team.feishu.cn/sheets/test"}).status_code == 400


def test_live_requires_key_and_rejects_cross_origin(tmp_path):
    settings = Settings(_env_file=None, mode="live", database=tmp_path / "db.sqlite", app_id="id", app_secret="secret", target_app_token="app", target_table_id="tbl", access_key="test-pass")
    with TestClient(create_app(settings)) as client:
        assert "访问口令" in client.get("/").text
        assert client.post("/api/batches/demo").status_code == 401
        assert client.post("/api/login", json={"access_key": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"access_key": "test-pass"}, headers={"Origin": "https://evil.test"}).status_code == 403
        assert client.post("/api/login", json={"access_key": "test-pass"}).status_code == 200
        assert client.post("/api/batches/demo").status_code == 200


def test_attachment_cache_preview_and_remove(tmp_path):
    app = create_app(Settings(_env_file=None, database=tmp_path/'db.sqlite'))
    with TestClient(app) as client:
        batch = client.post('/api/batches/demo').json()
        item_id = batch['items'][0]['id']
        path = f"/api/batches/{batch['id']}/items/{item_id}/attachments"
        response = client.post(path, data={'revision':batch['revision']}, files={'file':('../证据.png',b'PNG fixture','image/png')})
        assert response.status_code == 200
        saved = response.json()
        entry = saved['items'][0]['attachments'][0]
        assert entry['name'] == '证据.png'
        assert 'storage_name' not in entry and 'file_token' not in entry
        preview = client.get(path+'/'+entry['id'])
        assert preview.content == b'PNG fixture'
        response = client.request('DELETE',path+'/'+entry['id'],json={'revision':saved['revision']})
        assert response.status_code == 200
        assert not response.json()['items'][0]['attachments']
        assert not list((tmp_path/'uploads').iterdir())


def test_svg_and_oversize_uploads_rejected(tmp_path):
    settings = Settings(_env_file=None,database=tmp_path/'db.sqlite',max_attachment_mb=1)
    with TestClient(create_app(settings)) as client:
        batch = client.post('/api/batches/demo').json()
        path = f"/api/batches/{batch['id']}/items/{batch['items'][0]['id']}/attachments"
        for name, data, content_type in [('active.svg',b'<svg/>','image/svg+xml'),('large.png',b'x'*(1024*1024+1),'image/png')]:
            response = client.post(path,data={'revision':batch['revision']},files={'file':(name,data,content_type)})
            assert response.status_code == 400
        assert not list((tmp_path/'uploads').glob('*'))


def test_local_excel_can_be_imported_through_web(tmp_path):
    from io import BytesIO
    from openpyxl import Workbook
    book=Workbook();sheet=book.active
    sheet.append(['用例ID','PRD模块','标题','优先级','操作步骤','预期结果','执行结果','备注'])
    sheet.append(['FILE-1','相册','保存照片','P1','点击拍照','保存成功','fail','没有保存，必现'])
    payload=BytesIO();book.save(payload);book.close()
    with TestClient(create_app(Settings(_env_file=None,database=tmp_path/'db.sqlite'))) as client:
        response=client.post('/api/batches/file',files={'file':('result.xlsx',payload.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        assert response.status_code==200
        batch=response.json()
        assert len(batch['items'])==1
        assert batch['items'][0]['frequency']=='Very High / Frequent'
        assert 'problem_type' not in batch['defaults'] and 'frequency' not in batch['defaults']


def test_review_link_allows_only_one_batch_without_team_login(tmp_path):
    from fail2bug.access import issue_review_token
    settings=Settings(_env_file=None,mode='live',database=tmp_path/'db.sqlite',app_id='id',app_secret='secret',target_app_token='app',target_table_id='tbl',access_key='test-pass')
    app=create_app(settings)
    first=app.state.service.import_demo()
    second=app.state.service.import_demo()
    token=issue_review_token(settings,first['id'])
    with TestClient(app) as client:
        assert client.get(f"/api/batches/{first['id']}").status_code==401
        landing=client.get(f"/review/{first['id']}")
        assert landing.status_code==200 and first['title'] not in landing.text
        response=client.post(f"/api/review-access/{first['id']}",json={'token':token})
        assert response.status_code==200 and response.json()['path']==f"/batches/{first['id']}"
        assert all('HttpOnly' in cookie for cookie in response.headers.get_list('set-cookie'))
        assert '缺陷清单' in client.get(response.json()['path']).text
        assert client.get(f"/api/batches/{first['id']}").status_code==200
        assert client.get(f"/api/batches/{second['id']}").status_code==401
        assert client.post('/api/batches/demo').status_code==401
        assert '访问口令' in client.get('/').text
        from fail2bug.models import DraftEdit
        body={'revision':first['revision'],'defaults':{'version':'1.2.3'},'items':[{key:item.get(key,'') for key in DraftEdit.model_fields} for item in first['items']]}
        response=client.put(f"/api/batches/{first['id']}",json=body)
        assert response.status_code==200 and response.json()['defaults']['version']=='1.2.3'


def test_review_link_rejects_invalid_or_expired_tokens_and_other_origins(tmp_path):
    from fail2bug.access import issue_review_token
    settings=Settings(_env_file=None,mode='live',database=tmp_path/'db.sqlite',app_id='id',app_secret='secret',target_app_token='app',target_table_id='tbl',access_key='test-pass')
    app=create_app(settings)
    batch=app.state.service.import_demo()
    with TestClient(app) as client:
        path=f"/api/review-access/{batch['id']}"
        for token in ('invalid',issue_review_token(settings,batch['id'],now=0),issue_review_token(settings,'other-batch')):
            assert client.post(path,json={'token':token}).status_code==401
        token=issue_review_token(settings,batch['id'])
        assert client.post(path,json={'token':token},headers={'Origin':'https://other.test'}).status_code==403
        client.cookies.set('f2b_review',token)
        assert client.get('/api/batches/'+batch['id']+'-different').status_code==401
