"""Authenticated installed entry for the single React workbench."""
import hmac
import ipaddress
import os
import secrets

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict

from console.backend import app as console
from console.backend.tasks import TaskManager
from console.control.applications import Applications
from console.control.llm import ModelControl
from chatcopilot.evals.service import EvaluationServiceClient
from studio.setup import studio_environment
from studio.launcher import READINESS_PATH, register_readiness


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str


def application(home, framework, token):
    # Reuse routes and lifespan, not the module-global app state.
    app = FastAPI(title="AgentStrata Studio", lifespan=console.lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    cookie = secrets.token_urlsafe(32)
    environment = {**os.environ, **studio_environment(home)}
    app.state.tasks = TaskManager()
    app.state.evaluations = EvaluationServiceClient()
    app.state.llm = ModelControl(framework, environment=environment)
    app.state.applications = Applications(framework, home, model_path=app.state.llm.store.path)

    @app.middleware("http")
    async def local(request, call_next):
        try:
            local_peer = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local_peer = False
        if not local_peer or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "仅允许本机访问"}, 403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "仅允许同源请求"}, 403)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/login", READINESS_PATH}:
            if not hmac.compare_digest(request.cookies.get("studio", ""), cookie):
                return JSONResponse({"detail": "请使用本机启动入口打开工作台"}, 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        return JSONResponse({"detail": "输入格式无效，请检查字段长度和类型"}, 422)

    register_readiness(app, token)

    @app.post("/api/login")
    def login(body: Login):
        if not hmac.compare_digest(token, body.token):
            raise HTTPException(403, "请从本机启动器打开工作台")
        response = JSONResponse({"ok": True})
        response.set_cookie("studio", cookie, httponly=True, samesite="strict")
        return response

    # API routes precede the static mount. Their services resolve from request.app.
    app.router.routes.extend(console.app.router.routes)
    return app
