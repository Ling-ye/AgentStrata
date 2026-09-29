"""Console BFF for host-owned scheduled tasks; no Agent or provider execution."""
from __future__ import annotations

import ipaddress
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from chatcopilot.schedules.models import ScheduleError, ScheduleSettings
from console.backend.routes.common import get_instance
from console.control.schedules import service_for

router = APIRouter(prefix="/api/bots/{instance_id}", tags=["schedules"])


class CreateSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: ScheduleSettings
    request_id: str = Field(min_length=1, max_length=128)


class UpdateSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: ScheduleSettings
    revision: int = Field(ge=1, strict=True)


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    preview: bool = Field(default=True, strict=True)
    request_id: str = Field(min_length=1, max_length=128)


def _local(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def _mutation_access(request: Request) -> None:
    if request.client is None or not _local(request.client.host) or not _local(request.url.hostname):
        raise HTTPException(403, "定时任务写操作仅接受本机地址；远程使用本机 SSH 隧道")
    origin = request.headers.get("origin")
    if origin:
        source = urlsplit(origin)
        if source.scheme != request.url.scheme or source.netloc != request.headers.get("host"):
            raise HTTPException(403, "定时任务写操作要求同源请求")


def _call(instance_id, action):
    inst = get_instance(instance_id)
    try:
        return action(service_for(inst))
    except ScheduleError as exc:
        raise HTTPException({"not_found": 404, "conflict": 409, "unavailable": 503}.get(exc.code, 400),
                            detail={"code": exc.code, "message": str(exc)}) from exc
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "定时任务私有存储或实例配置不可用") from exc


@router.get("/schedules")
def overview(instance_id: str):
    return _call(instance_id, lambda service: service.overview())


@router.post("/schedules")
def create(instance_id: str, body: CreateSchedule, request: Request):
    _mutation_access(request)
    return _call(instance_id, lambda service: service.create(body.settings, request_id=body.request_id))


@router.put("/schedules/{task_id}")
def update(instance_id: str, task_id: str, body: UpdateSchedule, request: Request):
    _mutation_access(request)
    return _call(instance_id, lambda service: service.update(task_id, body.settings, revision=body.revision))


@router.delete("/schedules/{task_id}")
def delete(instance_id: str, task_id: str, request: Request, revision: int = Query(ge=1)):
    _mutation_access(request)
    _call(instance_id, lambda service: service.delete(task_id, revision=revision))
    return {"deleted": True}


@router.post("/schedules/{task_id}/runs")
def start_run(instance_id: str, task_id: str, body: StartRun, request: Request):
    _mutation_access(request)
    return _call(instance_id, lambda service: service.request_run(task_id, revision=body.revision,
        preview=body.preview, request_id=body.request_id))


@router.get("/schedule-runs")
def history(instance_id: str, task_id: str | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return _call(instance_id, lambda service: service.history(task_id=task_id, limit=limit, offset=offset))


@router.get("/schedule-runs/{run_id}")
def detail(instance_id: str, run_id: str):
    return _call(instance_id, lambda service: service.detail(run_id))


@router.post("/schedule-runs/{run_id}/cancel")
def cancel(instance_id: str, run_id: str, request: Request):
    _mutation_access(request)
    return _call(instance_id, lambda service: service.cancel(run_id))
