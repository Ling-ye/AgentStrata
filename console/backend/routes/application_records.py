"""Application-scoped history views over existing Evaluation and Harness owners."""
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, ConfigDict

from fastapi.responses import FileResponse
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from chatcopilot.evals.service import EvaluationServiceError
from console.backend.routes.applications import PrivateRoute, access, call, service
from console.backend.routes.common import get_evaluation_client, raise_evaluation_service_error
from console.backend.routes.harness import _call, _controller


class DesktopAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


router = APIRouter(prefix="/api/applications", tags=["applications"],
                   dependencies=[Depends(access)], route_class=PrivateRoute)


def repository_instance(request, application_id):
    row = call(lambda: service(request).require(application_id))
    if row["source"] != "repository":
        raise HTTPException(409, "此应用来源尚未接入测评与 Harness 历史")
    instance = row.get("instance_id")
    if not isinstance(instance, str) or not instance:
        raise HTTPException(409, "应用缺少明确的实例身份")
    return instance


@router.get("/{application_id}/evaluations")
def evaluations(request: Request, application_id: str,
                offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    bot_id = repository_instance(request, application_id)
    try:
        return get_evaluation_client(request).list(bot_id=bot_id, offset=offset, limit=limit)
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.get("/{application_id}/harness")
def harness(request: Request, application_id: str,
            page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    bot_id = repository_instance(request, application_id)
    return _call(lambda: _controller(request).list(page=page, limit=limit, bot_id=bot_id))


@router.get("/{application_id}/builds/{message}")
def desktop_build(request: Request, application_id: str, message: UUID):
    row = call(lambda: service(request).require(application_id))
    if row["source"] != "studio" or row["type"] not in {"desktop", "companion"}:
        raise HTTPException(409, "此应用没有桌面构建下载入口")
    artifact = call(lambda: service(request).studio().artifact(row["project_id"], message.hex))
    return FileResponse(artifact, media_type="application/octet-stream",
                        filename="AgentStrata-" + row["project_id"][:8] + ".exe")


@router.post("/{application_id}/builds/{message}/desktop/{action}")
def desktop_action(request: Request, application_id: str, message: UUID,
                   action: Literal["bind", "launch"], body: DesktopAction):
    from console.control.application_desktop import operate
    try:
        return call(lambda: operate(service(request), application_id, message.hex, action))
    except TimeoutError:
        raise HTTPException(409, "Desktop operation is already in progress") from None
