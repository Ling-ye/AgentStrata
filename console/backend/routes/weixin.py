"""Local Console API for personal Weixin channel binding."""

import ipaddress
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from chatcopilot.contracts.weixin import WeixinError
from console.backend.routes.common import get_instance, get_task_manager
from console.control.weixin import WeixinControl


router = APIRouter(prefix="/api/bots", tags=["weixin"])


class Verification(BaseModel):
    code: str = Field(min_length=1, max_length=32)


def _local_request(request: Request):
    def local(host):
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except (TypeError, ValueError):
            return False

    if request.client is None or not local(request.client.host) or not local(request.url.hostname):
        raise HTTPException(403, "微信绑定仅接受本机 Console 请求")
    origin = request.headers.get("origin")
    if origin and (
        urlsplit(origin).scheme != request.url.scheme
        or urlsplit(origin).netloc != request.headers.get("host")
    ):
        raise HTTPException(403, "微信绑定要求同源请求")


def _control(request):
    if not hasattr(request.app.state, "weixin"):
        request.app.state.weixin = WeixinControl()
    return request.app.state.weixin


def _error(error):
    code = error.code if isinstance(error, WeixinError) else "weixin_configuration_unavailable"
    return HTTPException(404 if code == "weixin_login_not_found" else 409, detail={"code": code})


@router.get("/{instance_id}/channels/weixin/status")
async def status(request: Request, instance_id: str):
    _local_request(request)
    try:
        return await _control(request).status(get_instance(instance_id))
    except (WeixinError, ValueError, OSError) as error:
        raise _error(error) from None


@router.post("/{instance_id}/channels/weixin/login")
async def start(request: Request, instance_id: str):
    _local_request(request)
    try:
        return _control(request).start(get_instance(instance_id), get_task_manager(request))
    except (WeixinError, ValueError, OSError, RuntimeError) as error:
        raise _error(error) from None


@router.get("/{instance_id}/channels/weixin/login/{login_id}")
async def login_status(request: Request, instance_id: str, login_id: str):
    _local_request(request)
    try:
        return _control(request).login.get(instance_id, login_id).public()
    except WeixinError as error:
        raise _error(error) from None


@router.post("/{instance_id}/channels/weixin/login/{login_id}/verify")
async def verify(request: Request, instance_id: str, login_id: str, body: Verification):
    _local_request(request)
    try:
        return _control(request).login.verify(instance_id, login_id, body.code)
    except WeixinError as error:
        raise _error(error) from None


@router.delete("/{instance_id}/channels/weixin/login/{login_id}")
async def cancel(request: Request, instance_id: str, login_id: str):
    _local_request(request)
    try:
        return await _control(request).login.cancel(instance_id, login_id)
    except WeixinError as error:
        raise _error(error) from None
