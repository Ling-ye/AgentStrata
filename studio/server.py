"""Loopback-only developer web surface. Durable work continues across tab closure."""
from contextlib import asynccontextmanager
import asyncio
import hmac
from pathlib import Path
import secrets
import sqlite3
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from studio.workspace import Studio


class Create(BaseModel):
    id: UUID
    live: bool = False


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
        if run_worker:
            await asyncio.to_thread(guide.start)
        tasks = {}
        async def poll():
            while True:
                for key in studio.pending():
                    if key not in tasks or tasks[key].done():
                        tasks[key] = asyncio.create_task(asyncio.to_thread(studio.step, key))
                tasks_copy = list(tasks.items())
                for key, task in tasks_copy:
                    if task.done():
                        task.result()
                        tasks.pop(key, None)
                await asyncio.sleep(1)
        worker = asyncio.create_task(poll()) if run_worker else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await asyncio.gather(*tasks.values(), return_exceptions=True)
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
        if request.url.path.startswith("/api/") and request.url.path != "/api/login":
            if not hmac.compare_digest(request.cookies.get("studio", ""), cookie):
                return JSONResponse({"detail":"请使用本机启动入口打开工作台"}, 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail":str(exc)}, 400)

    @app.exception_handler(KeyError)
    async def missing(_request, _exc):
        return JSONResponse({"detail":"项目或安装包不存在"}, 404)

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
        return {"id":studio.create(body.id.hex, live=body.live)}

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
        return FileResponse(studio.artifact(project.hex, message.hex), media_type="application/octet-stream",
                            filename="AgentStrata-" + project.hex[:8] + ".exe")

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
        with studio.db.connect(write=True) as db:
            row = db.execute("SELECT m.*,p.version AS current FROM messages m JOIN projects p ON p.id=m.project "
                             "WHERE m.id=? AND p.id=?", (message.hex, project.hex)).fetchone()
            if not row or row["state"] != "failed" or row["version"] != row["current"]:
                raise HTTPException(409, "原任务无法在当前版本重试，请提出新的改进")
            if not studio.retryable(project.hex, row):
                raise HTTPException(409, "旧任务及证据保持只读，请在当前聊天发送新的开发请求。")
            try:
                db.execute("UPDATE messages SET state=?,reply='' WHERE id=?", (
                    "starting" if row["task"] or row["goal"] else "thinking", message.hex))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "当前已有其他改动正在处理") from exc
        return {"ok": True}

    from studio.setup import routes
    app.include_router(routes(home, framework, guide))
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="studio")
    return app
