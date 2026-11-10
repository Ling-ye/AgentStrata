"""Local application workbench BFF; explicit operations, never proxy arbitrary commands."""
from __future__ import annotations

import ipaddress
import sqlite3
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from console.control.applications import Applications


def access(request: Request):
    try:
        local = request.client and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    if not local or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, "应用工作台仅允许本机访问")
    origin = request.headers.get("origin")
    if origin and (urlsplit(origin).netloc != request.headers.get("host") or
                   urlsplit(origin).scheme != request.url.scheme):
        raise HTTPException(403, "应用操作要求同源请求")


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, "输入格式无效，请检查字段长度和类型") from None
        return handler


router = APIRouter(prefix="/api/applications", tags=["applications"],
                   dependencies=[Depends(access)], route_class=PrivateRoute)


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    type: str = "qq_official"


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(default="", max_length=8000)
    app_id: str = Field(default="", max_length=32)
    app_secret: str = Field(default="", max_length=512)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    text: str = Field(min_length=1, max_length=16000)


def service(request):
    manager = getattr(request.app.state, "applications", None)
    if manager is None:
        models = getattr(request.app.state, "llm", None)
        manager = Applications(model_path=models.store.path if models else None)
        request.app.state.applications = manager
    return manager


def call(operation):
    try:
        return operation()
    except KeyError:
        raise HTTPException(404, "应用或记录不存在") from None
    except PermissionError:
        raise HTTPException(409, "此应用尚不支持该操作") from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "当前应用已有操作正在处理") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "本机应用数据暂不可用") from None


@router.get("")
def listing(request: Request):
    return call(lambda: {"applications": service(request).catalog()})


@router.post("")
def create(request: Request, body: Create):
    return call(lambda: service(request).create(body.id.hex, body.type))


@router.get("/{application_id}")
def detail(request: Request, application_id: str):
    return call(lambda: service(request).detail(application_id))


@router.get("/{application_id}/configuration")
def configuration(request: Request, application_id: str):
    return call(lambda: service(request).configuration(application_id))


@router.post("/{application_id}/configuration")
def configure(request: Request, application_id: str, body: Configuration):
    return call(lambda: service(request).configure(application_id, body.model_dump()))


@router.post("/{application_id}/check")
def check(request: Request, application_id: str):
    return call(lambda: service(request).action(application_id, "check"))


@router.post("/{application_id}/start")
def start(request: Request, application_id: str):
    return call(lambda: service(request).action(application_id, "start"))


@router.post("/{application_id}/stop")
def stop(request: Request, application_id: str):
    return call(lambda: service(request).action(application_id, "stop"))


@router.get("/{application_id}/messages")
def messages(request: Request, application_id: str):
    return call(lambda: service(request).messages(application_id))


@router.post("/{application_id}/messages")
def submit(request: Request, application_id: str, body: Message, background: BackgroundTasks):
    manager = service(request)
    call(lambda: manager.submit(application_id, body.id.hex, body.text))
    background.add_task(manager.process_message, application_id, body.id.hex)
    return {"id": body.id.hex}


@router.post("/{application_id}/proposals/{message}/apply")
def apply(request: Request, application_id: str, message: UUID):
    return call(lambda: service(request).apply(application_id, message.hex))



def preview(request):
    from console.control.application_preview import Preview
    return Preview(service(request))


@router.get("/{application_id}/chat")
def chat(request: Request, application_id: str):
    return call(lambda: preview(request).operate(application_id))


@router.post("/{application_id}/chat")
def chat_send(request: Request, application_id: str, body: Message):
    return call(lambda: preview(request).operate(application_id, "send", body.id.hex, body.text))


@router.post("/{application_id}/chat/{message}/retry")
def chat_retry(request: Request, application_id: str, message: UUID):
    return call(lambda: preview(request).operate(application_id, "retry", message.hex))


@router.post("/{application_id}/chat/{message}/abort")
def chat_abort(request: Request, application_id: str, message: UUID):
    return call(lambda: preview(request).operate(application_id, "abort", message.hex))


@router.get("/{application_id}/improvement-readiness")
def improvement_readiness(request: Request, application_id: str):
    from console.control.application_readiness import improvement_readiness as inspect_source
    return call(lambda: inspect_source(service(request), application_id))
