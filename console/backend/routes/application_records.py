"""Application-scoped history views over existing Evaluation and Harness owners."""
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from studio.downloads import VerifiedBuildResponse

from chatcopilot.evals.service import EvaluationServiceError
from console.backend.routes.applications import PrivateRoute, access, call, service
from console.backend.routes.common import get_evaluation_client, raise_evaluation_service_error
from console.backend.routes.harness import _call, _controller


class DesktopAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


router = APIRouter(prefix="/api/applications", tags=["applications"],
                   dependencies=[Depends(access)], route_class=PrivateRoute)


def repository_instance(request: Request, application_id: str):
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
    if row["source"] != "studio" or row["type"] not in {"desktop", "companion", "web", "source"}:
        raise HTTPException(409, "此应用没有桌面构建下载入口")
    def response():
        artifact, digest = service(request).studio().artifact_reference(row["project_id"], message.hex)
        return VerifiedBuildResponse(artifact, digest, filename="AgentStrata-" + row["project_id"][:8] + artifact.suffix)
    return call(response)


@router.post("/{application_id}/builds/{message}/desktop/{action}")
def desktop_action(request: Request, application_id: str, message: UUID,
                   action: Literal["bind", "launch"], body: DesktopAction):
    from console.control.application_desktop import operate
    try:
        return call(lambda: operate(service(request), application_id, message.hex, action))
    except TimeoutError:
        raise HTTPException(409, "Desktop operation is already in progress") from None


class InstallerBuild(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["windows-x64", "linux-x64"]
    kind: Literal["commit", "candidate"]
    revision: str


@router.get("/{application_id}/open")
def open_local_release(request: Request, application_id: str):
    from console.control import released_applications
    apps = service(request)
    row = call(lambda: apps.require(application_id))
    if row["source"] != "release":
        raise HTTPException(404, "该应用没有已接入的本机服务入口")
    try:
        url = released_applications.open_url(apps, row)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


@router.get("/{application_id}/installers")
def installers(request: Request, application_id: str):
    from console.control import application_installers
    apps = service(request)
    return call(lambda: application_installers.catalog(apps, apps.require(application_id)))


@router.get("/{application_id}/installer-plan")
def installer_plan(request: Request, application_id: str, kind: Literal["commit", "candidate"], revision: str):
    from console.control import application_installers
    apps = service(request)
    return _call(lambda: call(lambda: application_installers.selection_plan(
        apps, apps.require(application_id), kind, revision, lambda: _controller(request))))


@router.post("/{application_id}/installers", status_code=202)
def build_installer(request: Request, application_id: str, body: InstallerBuild, background: BackgroundTasks):
    from console.control import application_installers
    apps = service(request)
    key, job = _call(lambda: call(lambda: application_installers.register(
        apps, apps.require(application_id), body.platform, body.kind, body.revision, lambda: _controller(request))))
    if job:
        background.add_task(job)
    return {"id": key}


class InstallationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format: Literal["agentstrata-install/v1"]
    application: str
    platform: Literal["windows-x64", "linux-x64"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: int = Field(gt=0)
    services_ready: bool


@router.post("/{application_id}/installers/{key}/{platform}/installation")
def import_installation(request: Request, application_id: str, key: str,
                        platform: Literal["windows-x64", "linux-x64"], body: InstallationReceipt):
    from console.control import application_installers
    apps = service(request)
    return call(lambda: application_installers.import_installation(apps, apps.require(application_id), key, platform, body))


@router.get("/{application_id}/installers/{key}/{platform}")
def download_installer(request: Request, application_id: str, key: str, platform: Literal["windows-x64", "linux-x64"]):
    from console.control import application_installers
    apps = service(request)
    return call(lambda: application_installers.download(apps, apps.require(application_id), key, platform))
