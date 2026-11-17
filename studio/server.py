"""Loopback-only developer web surface. Durable work continues across tab closure."""
from contextlib import asynccontextmanager
import asyncio
import hmac
from pathlib import Path
import secrets
import sqlite3
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from studio.workspace import Studio
from studio.downloads import VerifiedBuildResponse
from studio.launcher import READINESS_PATH, register_readiness


class Create(BaseModel):
    id: UUID
    live: bool = False
    application_type: str = "desktop"


class Message(BaseModel):
    id: UUID
    text: str


def application(home, framework, token, *, run_worker=True):
    studio = Studio(home, framework)
    cookie = secrets.token_urlsafe(32)
    from studio.setup import Guide
    guide = Guide(home, framework)

    @asynccontextmanager
    async def lifespan(app):
        from studio.worker import StudioWorker
        consumer = StudioWorker(studio)
        consumer.guide = guide
        worker = asyncio.create_task(consumer.run()) if run_worker else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await asyncio.to_thread(guide.stop)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.studio = studio

    @app.middleware("http")
    async def local(request, call_next):
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail":"仅允许本机访问"}, 403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail":"仅允许同源请求"}, 403)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/login", READINESS_PATH}:
            if not hmac.compare_digest(request.cookies.get("studio", ""), cookie):
                return JSONResponse({"detail":"请使用本机启动入口打开工作台"}, 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_fields(_request, _exc):
        # Validation errors must not echo raw credential fields into responses/logs.
        return JSONResponse({"detail": "输入格式无效，请检查字段长度和类型"}, 422)

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail":str(exc)}, 400)

    @app.exception_handler(KeyError)
    async def missing(_request, _exc):
        return JSONResponse({"detail":"项目或安装包不存在"}, 404)

    register_readiness(app, token)

    @app.post("/api/login")
    def login(body: dict):
        supplied = body.get("token")
        if not isinstance(supplied, str) or not hmac.compare_digest(token, supplied):
            raise HTTPException(403, "请从本机启动器打开工作台")
        response = JSONResponse({"ok":True})
        response.set_cookie("studio", cookie, httponly=True, samesite="strict")
        return response

    @app.get("/api/projects")
    def projects():
        return studio.projects()

    @app.post("/api/projects")
    def create(body: Create):
        return {"id":studio.create(body.id.hex, live=body.live, application_type=body.application_type)}

    @app.get("/api/projects/{project}/messages")
    def messages(project: UUID):
        return studio.conversation(project.hex)

    @app.post("/api/projects/{project}/messages")
    def submit(project: UUID, body: Message):
        try:
            studio.submit(project.hex, body.id.hex, body.text)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "当前应用已有改动正在处理") from exc
        return {"id":body.id.hex}

    @app.get("/api/projects/{project}/builds/{message}")
    def download(project: UUID, message: UUID):
        path, digest = studio.artifact_reference(project.hex, message.hex)
        return VerifiedBuildResponse(path, digest, filename="AgentStrata-" + project.hex[:8] + ".exe")

    @app.post("/api/projects/{project}/builds/{message}/try")
    def trial(project: UUID, message: UUID):
        from studio.delivery import windows_path
        import subprocess
        # The requested operation is opening the exact verified installer; installation remains interactive.
        path = studio.artifact(project.hex, message.hex)
        subprocess.Popen(["explorer.exe", windows_path(path)])
        return {"message":"已请求 Windows 打开这份安装包。"}

    @app.post("/api/projects/{project}/builds/{message}/observe")
    def observe(project: UUID, message: UUID):
        import json
        import subprocess
        import time
        from studio.delivery import windows_path
        file = studio.artifact(project.hex, message.hex)
        receipt = json.loads((file.parent / "build-receipt.json").read_text())
        result = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", windows_path(framework / "integrations/desktop_app/observe.ps1"), "-InstallId", receipt["install_id"],
            "-SourceDigest", receipt["source_digest"]], text=True, timeout=90)
        loaded = json.loads(result.strip()) is True
        if loaded:
            with studio.db.connect(write=True) as db:
                db.execute("UPDATE messages SET launched=? WHERE id=? AND project=?", (time.time(), message.hex, project.hex))
        return {"loaded": loaded}

    @app.post("/api/projects/{project}/messages/{message}/retry")
    def retry(project: UUID, message: UUID):
        try:
            studio.retry(project.hex, message.hex)
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise HTTPException(409, str(exc) if isinstance(exc, ValueError) else "当前已有其他改动正在处理") from exc
        return {"ok": True}

    from studio.workbench_api import application_routes
    app.include_router(application_routes(studio))

    from studio.setup import routes
    app.include_router(routes(home, framework, guide))
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="studio")
    return app
