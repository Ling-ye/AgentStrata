"""Local workbench; same-origin access precedes file reads and job submission."""
from __future__ import annotations

from contextlib import asynccontextmanager
import base64
from importlib.resources import files
import json
from pathlib import Path
import secrets
import tempfile

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .config import source_notice
from .runtime import JobRuntime
from .types import GenerationError, GenerationRequest, INPUT_SUFFIXES, MAX_UPLOAD


def create_app(runtime: JobRuntime) -> FastAPI:
    token = secrets.token_urlsafe(32)
    port = runtime.config.port
    hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    origins = {"http://" + host for host in hosts}

    @asynccontextmanager
    async def lifespan(app):
        with runtime:
            yield

    app = FastAPI(title="Live2D Studio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        if request.headers.get("host") not in hosts or (
                request.headers.get("origin") and request.headers["origin"] not in origins):
            return JSONResponse({"error": "仅支持本机同源访问"}, status_code=403)
        if request.url.path.startswith("/api/") and not secrets.compare_digest(
                request.headers.get("x-generator-token", ""), token):
            return JSONResponse({"error": "工作台会话失效，请刷新页面"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; object-src 'none'; frame-ancestors 'none'"})
        return response

    @app.exception_handler(GenerationError)
    async def invalid(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing(request, exc):
        return JSONResponse({"error": "任务或产物不存在"}, status_code=404)

    @app.exception_handler(OSError)
    async def io_failure(request, exc):
        return JSONResponse({"error": "文件操作失败；检查本机目录权限和可用空间"}, status_code=400)

    @app.get("/")
    def index():
        html = files(__package__).joinpath("resources/index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__SESSION_TOKEN__", token))

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in {"app.js", "style.css"}:
            return Response(status_code=404)
        media = "text/javascript" if name.endswith(".js") else "text/css"
        return Response(files(__package__).joinpath("resources", name).read_bytes(), media_type=media)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/legal")
    def legal():
        return source_notice()

    @app.get("/api/status")
    def status():
        return {"capabilities": runtime.config.capabilities(), "jobs": runtime.list(), "legal": source_notice()}

    @app.post("/api/jobs")
    async def submit(request: Request):
        limit = MAX_UPLOAD * 4 // 3 + 65536
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return JSONResponse({"error": "请求体积超限（文件最多 48 MiB）"}, status_code=413)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) - {"request", "input", "character_id"}:
                raise GenerationError("请求字段无效")
            value = GenerationRequest.parse(payload["request"])
            upload = payload.get("input")
            with tempfile.TemporaryDirectory(prefix=".upload-", dir=runtime.repository.root) as temporary:
                source = None
                if upload is not None:
                    if not isinstance(upload, dict) or set(upload) != {"filename", "data"}:
                        raise GenerationError("上传字段无效")
                    suffix = Path(upload["filename"]).suffix.lower()
                    if suffix not in INPUT_SUFFIXES:
                        raise GenerationError("不支持的文件类型")
                    data = base64.b64decode(upload["data"], validate=True)
                    if not data or len(data) > MAX_UPLOAD:
                        raise GenerationError("文件为空或超过 48 MiB")
                    source = Path(temporary) / ("input" + suffix)
                    source.write_bytes(data)
                return runtime.start(value, source, character_id=payload.get("character_id"))
        except (ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, GenerationError):
                raise
            raise GenerationError("请求 JSON 或文件数据格式无效") from None

    @app.get("/api/jobs/{identifier}")
    def job(identifier: str):
        return runtime.get(identifier)

    @app.post("/api/jobs/{identifier}/retry")
    def retry(identifier: str):
        return runtime.retry(identifier)

    @app.post("/api/jobs/{identifier}/cancel")
    def cancel(identifier: str):
        return runtime.cancel(identifier)

    @app.get("/api/jobs/{identifier}/{artifact}")
    def download(identifier: str, artifact: str):
        path = runtime.artifact(identifier, artifact)
        return FileResponse(path, media_type="image/png" if artifact == "preview.png" else "application/zip",
                            filename=None if artifact == "preview.png" else f"live2d-{identifier[:8]}.zip")

    from .character_web import register
    register(app, runtime)
    return app
