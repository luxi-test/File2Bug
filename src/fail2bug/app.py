import hmac
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from uuid import uuid4
from tempfile import TemporaryDirectory
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .access import review_token_remaining
from .feishu import FeishuClient, FeishuError
from .models import ReviewUpdate
from .management import Configuration, ConfigUpdate, FIELD_LABELS, Services
from .parser import InputError
from .service import Service
from .store import Conflict, Store

ROOT = Path(__file__).parent


class ImportRequest(BaseModel):
    url: str = Field(min_length=1, max_length=3000)


class SubmitRequest(BaseModel):
    revision: int


class LoginRequest(BaseModel):
    access_key: str = Field(max_length=1000)


class ReviewAccessRequest(BaseModel):
    token: str = Field(max_length=200)


def public_batch(batch: dict) -> dict:
    result = {key: value for key, value in batch.items() if key != "target"}
    result["items"] = []
    for item in batch["items"]:
        visible = {key: value for key, value in item.items() if key not in {"write_payload", "operation_id", "pending_metadata_snapshot"}}
        visible["attachments"] = [{key: value for key, value in entry.items() if key not in {"file_token", "storage_name"}} for entry in item.get("attachments", [])]
        result["items"].append(visible)
    return result


def local_management_request(request: Request) -> bool:
    # 同时检查实际连接和 Host；不信任代理头，防止转发及 DNS rebinding。
    if not request.client or any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host")):
        return False
    try:
        loopback = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False
    return loopback and request.url.hostname in {"localhost", "127.0.0.1", "::1"}


def create_app(settings: Settings | None = None, client=None, *, configuration=None) -> FastAPI:
    settings = settings or Settings()
    store = Store(settings.database)
    configuration = configuration or Configuration(settings, store)
    services = Services(configuration, client)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        yield
        services.close()

    app = FastAPI(title="Fail2Bug", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.configuration = configuration
    app.state.service = services.current()
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    def current_service():
        with services.acquire() as service:
            app.state.service = service
            yield service

    def authorized(request):
        if settings.mode == "demo":
            return True
        actual = request.cookies.get("f2b_access", "")
        if hmac.compare_digest(actual.encode(), settings.access_key.get_secret_value().encode()):
            return True
        parts = request.url.path.strip('/').split('/')
        batch_id = parts[1] if len(parts) == 2 and parts[0] == 'batches' else parts[2] if len(parts) >= 3 and parts[:2] == ['api', 'batches'] else ''
        return bool(batch_id and review_token_remaining(settings, batch_id, request.cookies.get('f2b_review', '')))

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        admin_path = request.url.path == "/admin" or request.url.path == "/api/admin" or request.url.path.startswith("/api/admin/")
        if admin_path and not local_management_request(request):
            return JSONResponse({"detail": "管理页仅允许本机通过 http://127.0.0.1:8000/admin 访问。"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "请求来源不匹配，请从工具页面操作。"}, status_code=403)
        public_api = request.url.path == '/api/login' or request.url.path.startswith('/api/review-access/')
        if request.url.path.startswith("/api/") and not public_api and not authorized(request):
            return JSONResponse({"detail": "请先输入访问口令。"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if admin_path:
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith("/api/admin"):
            # FastAPI 默认错误包含 input；绝不把包含新密钥的请求体回显。
            return JSONResponse({"detail": "配置格式不正确，请检查字段内容与长度。"}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(InputError)
    async def input_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(FeishuError)
    async def feishu_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "没有找到此批次，请检查链接。"}, status_code=404)
        return HTMLResponse("没有找到此批次。<a href='/'>返回首页</a>", status_code=404)

    def render(request, template, **context):
        active = configuration.snapshot()[1]
        return templates.TemplateResponse(request=request, name=template, context={"mode": active.mode, "source_url": active.source_url,
            "show_admin": local_management_request(request) and authorized(request), **context})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        if not authorized(request):
            return render(request, "login.html")
        return render(request, "admin.html", admin_page=True, field_labels=FIELD_LABELS)

    @app.get("/api/admin/config")
    def admin_config():
        return configuration.public()

    @app.post("/api/admin/check")
    def admin_check(body: ConfigUpdate):
        return configuration.check(configuration.candidate(body))

    @app.put("/api/admin/config")
    def admin_save(body: ConfigUpdate):
        result = configuration.save(body)
        app.state.service = services.current()
        return result

    @app.post("/api/login")
    def login(body: LoginRequest):
        if not hmac.compare_digest(body.access_key.encode(), settings.access_key.get_secret_value().encode()):
            return JSONResponse({"detail": "访问口令不正确。"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie("f2b_access", body.access_key, httponly=True, samesite="strict", secure=settings.base_url.startswith("https://"))
        return response

    @app.get('/review/{batch_id}', response_class=HTMLResponse)
    def review_link(request: Request, batch_id: str):
        return render(request, 'review-link.html', batch_id=batch_id)

    @app.post('/api/review-access/{batch_id}')
    def review_access(batch_id: str, body: ReviewAccessRequest):
        remaining = review_token_remaining(settings, batch_id, body.token)
        if not remaining:
            return JSONResponse({'detail': '审核链接无效或已过期，请回到机器人发送“审核链接”获取新链接。'}, status_code=401)
        store.get(batch_id)
        response = JSONResponse({'path': f'/batches/{batch_id}'})
        for path in (f'/batches/{batch_id}', f'/api/batches/{batch_id}'):
            response.set_cookie('f2b_review', body.token, max_age=remaining, path=path, httponly=True, samesite='strict', secure=settings.base_url.startswith('https://'))
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not authorized(request):
            return render(request, "login.html")
        recent = [public_batch(batch) for batch in store.recent()]
        return render(request, "index.html", recent=recent)

    @app.get("/batches/{batch_id}", response_class=HTMLResponse)
    def batch_page(request: Request, batch_id: str, service: Service = Depends(current_service)):
        if not authorized(request):
            return render(request, "login.html")
        return render(request, "batch.html", batch=public_batch(service.review(batch_id)), max_attachment_mb=service.settings.max_attachment_mb)

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str, service: Service = Depends(current_service)):
        return public_batch(service.review(batch_id))

    @app.post("/api/batches/demo")
    def demo(service: Service = Depends(current_service)):
        return public_batch(service.import_demo())

    @app.post('/api/batches/file')
    async def import_file(file: UploadFile = File(...), sheet: str = Form(''), service: Service = Depends(current_service)):
        name=(file.filename or '').replace('\\','/').split('/')[-1]
        if Path(name).suffix.lower() not in {'.xlsx','.zip'}:
            raise InputError('请选择 .xlsx 或 .zip 文件。')
        try:
            with TemporaryDirectory(prefix='f2b-web-import-') as temp:
                path=Path(temp)/name
                size=0
                with path.open('xb') as handle:
                    path.chmod(0o600)
                    while chunk := await file.read(1024*1024):
                        size += len(chunk)
                        if size > service.settings.max_import_mb*1024*1024:
                            raise InputError('文件超过本地导入大小上限。')
                        handle.write(chunk)
                batch=await run_in_threadpool(service.import_file,path,sheet=sheet.strip() or None)
                return public_batch(batch)
        finally:
            await file.close()

    @app.post("/api/batches")
    def import_sheet(body: ImportRequest, service: Service = Depends(current_service)):
        return public_batch(service.import_sheet(body.url))

    @app.put("/api/batches/{batch_id}")
    def update_batch(batch_id: str, body: ReviewUpdate, service: Service = Depends(current_service)):
        return public_batch(service.update(batch_id, body))

    @app.post("/api/batches/{batch_id}/submit")
    def submit_batch(batch_id: str, body: SubmitRequest, service: Service = Depends(current_service)):
        return public_batch(service.submit(batch_id, body.revision))

    @app.post("/api/batches/{batch_id}/items/{item_id}/attachments")
    async def add_attachment(batch_id: str, item_id: str, file: UploadFile = File(...), revision: int = Form(...), service: Service = Depends(current_service)):
        settings = service.settings
        batch = store.get(batch_id)
        if batch["mode"] == "live" and not settings.field_attachments:
            raise InputError("尚未配置目标附件字段。")
        if not any(item["id"] == item_id for item in batch["items"]):
            raise KeyError(item_id)
        content_type = file.content_type or "application/octet-stream"
        suffix=Path(file.filename or '').suffix.lower()
        if content_type=='application/octet-stream' and suffix in {'.log','.txt','.pdf'}:
            content_type='application/pdf' if suffix=='.pdf' else 'text/plain'
        if (not content_type.startswith(("image/", "video/")) and content_type not in {'application/pdf','text/plain'}) or content_type == "image/svg+xml":
            raise InputError("请选择图片、视频、PDF、TXT 或日志文件（不支持 SVG）。")
        name = (file.filename or "附件").replace("\\", "/").split("/")[-1].replace("\x00", "")[:200]
        if name.lower().endswith(".svg"):
            raise InputError("不支持上传 SVG，请使用 PNG 或 JPG。")
        storage_name = uuid4().hex
        path = service.attachment_path(storage_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with path.open("xb") as handle:
                path.chmod(0o600)
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_attachment_mb * 1024 * 1024:
                        raise InputError(f"单个文件不能超过 {settings.max_attachment_mb}MB。")
                    handle.write(chunk)
            if not size:
                raise InputError("附件为空，请重新选择文件。")
            entry = {"id": str(uuid4()), "name": name, "content_type": content_type, "size": size, "storage_name": storage_name, "linked": False}
            result = service.add_attachment(batch_id, item_id, entry, revision)
            return public_batch(result)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.get("/api/batches/{batch_id}/items/{item_id}/attachments/{attachment_id}")
    def preview_attachment(batch_id: str, item_id: str, attachment_id: str, service: Service = Depends(current_service)):
        item = next((item for item in store.get(batch_id)["items"] if item["id"] == item_id), None)
        entry = next((entry for entry in (item or {}).get("attachments", []) if entry["id"] == attachment_id), None)
        if not entry:
            raise KeyError(attachment_id)
        path = service.attachment_path(entry["storage_name"])
        if not path.is_file():
            raise KeyError(attachment_id)
        disposition='inline' if entry['content_type'].startswith(('image/','video/')) else 'attachment'
        return FileResponse(path, media_type=entry["content_type"], filename=entry["name"], content_disposition_type=disposition)

    @app.delete("/api/batches/{batch_id}/items/{item_id}/attachments/{attachment_id}")
    def remove_attachment(batch_id: str, item_id: str, attachment_id: str, body: SubmitRequest, service: Service = Depends(current_service)):
        return public_batch(service.remove_attachment(batch_id, item_id, attachment_id, body.revision))

    return app
