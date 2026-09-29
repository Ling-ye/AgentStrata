"""Model configuration BFF; provider clients are assembled outside the UI."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from chatcopilot.core.model_settings import ModelSettingsConflict, ModelSettingsError

router = APIRouter(prefix="/api/llm", tags=["llm"])


class SaveModels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    connections: dict
    profiles: dict
    bindings: dict


class RefreshModels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection: str
    draft: dict | None = None


def _access(request):
    try:
        local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise HTTPException(403, "模型配置操作仅接受本机请求")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "模型配置操作要求同源请求")


def _service(request):
    return request.app.state.llm


def _call(operation):
    try:
        return operation()
    except ModelSettingsConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ModelSettingsError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "模型配置文件暂不可用") from exc


@router.get("/config")
def configuration(request: Request):
    return _call(_service(request).configuration)


@router.put("/config")
def save(request: Request, body: SaveModels):
    _access(request)
    return _call(lambda: _service(request).save(body.model_dump(exclude={"revision"}), body.revision))


@router.get("/catalog")
def catalog(request: Request, connection: str):
    return _call(lambda: _service(request).models(connection))


@router.post("/catalog/refresh")
def refresh(request: Request, body: RefreshModels):
    _access(request)
    return _call(lambda: _service(request).models(body.connection, refresh=True, draft=body.draft))
