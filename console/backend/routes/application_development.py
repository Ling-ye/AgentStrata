"""Local-only development operations, never arbitrary source paths or commands."""
import sqlite3
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from chatcopilot.evals.service import EvaluationServiceError
from chatcopilot.harness.models import HarnessError
from console.backend.routes.applications import PrivateRoute, access, service
from console.control.application_development import Development

router = APIRouter(prefix="/api/applications", tags=["applications"],
                   dependencies=[Depends(access)], route_class=PrivateRoute)
TaskId = Annotated[str, Path(pattern=r"^repair-[a-f0-9]{32}$")]


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    goal: str = Field(min_length=1, max_length=16000)


def invoke(request, operation):
    try:
        return operation(Development(service(request)))
    except KeyError:
        raise HTTPException(404, "应用或开发任务不存在") from None
    except PermissionError:
        raise HTTPException(409, "此应用来源不支持该开发操作") from None
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "本机开发数据暂不可用，原记录已保留") from None
    except (ValueError, RuntimeError, HarnessError, EvaluationServiceError):
        raise HTTPException(409, "开发服务未就绪或任务尚不满足验收条件；原记录已保留") from None


@router.get("/{application_id}/development")
def status(request: Request, application_id: str):
    return invoke(request, lambda host: host.status(application_id))


@router.post("/{application_id}/development/prepare")
def prepare(request: Request, application_id: str):
    return invoke(request, lambda host: host.prepare(application_id))


@router.get("/{application_id}/development/tasks")
def tasks(request: Request, application_id: str, page: int = Query(1, ge=1)):
    return invoke(request, lambda host: host.tasks(application_id, page))


@router.post("/{application_id}/development/tasks")
def start(request: Request, application_id: str, body: Start):
    return invoke(request, lambda host: host.start(application_id, body.id.hex, body.goal))


@router.get("/{application_id}/development/tasks/{task_id}")
def get(request: Request, application_id: str, task_id: TaskId):
    return invoke(request, lambda host: host.get(application_id, task_id))


@router.post("/{application_id}/development/tasks/{task_id}/cancel")
def cancel(request: Request, application_id: str, task_id: TaskId):
    return invoke(request, lambda host: host.cancel(application_id, task_id))


@router.post("/{application_id}/development/tasks/{task_id}/install")
def install(request: Request, application_id: str, task_id: TaskId):
    return invoke(request, lambda host: host.install(application_id, task_id))
