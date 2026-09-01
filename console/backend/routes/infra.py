from __future__ import annotations

from ipaddress import ip_address
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

from console.backend.routes.common import get_task_manager
from console.backend.sse import sse
from console.control import operations, services

router = APIRouter(prefix="/api/infra", tags=["infra"])


def _require_loopback_request(request: Request) -> None:
    client = request.client
    try:
        address = ip_address(client.host if client is not None else "")
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise HTTPException(
            status_code=403,
            detail="NapCat WebUI token is available only from a loopback Console request",
        )


@router.get("")
def infra_list():
    return [
        s for s in services.all_services_status()
        if s.get("service_type") in ("compose", "standalone")
    ]


@router.post("/compose-up")
def infra_compose_up():
    """Initialize / start all docker-compose services at once."""
    res = services.compose_up_all()
    if not res.get("ok"):
        raise HTTPException(
            status_code=409,
            detail=res.get("error") or res.get("stderr") or "docker compose up failed",
        )
    return res


@router.post("/{service_id}/start")
def infra_start(service_id: str):
    return _infra_action(service_id, "start")


@router.post("/{service_id}/stop")
def infra_stop(service_id: str):
    return _infra_action(service_id, "stop")


@router.post("/{service_id}/restart")
def infra_restart(service_id: str):
    return _infra_action(service_id, "restart")


@router.post("/{service_id}/pull")
def infra_pull(request: Request, service_id: str):
    svc = _resolve_infra(service_id)
    if svc.service_type != "compose":
        raise HTTPException(status_code=400, detail="pull is supported only for compose services")
    try:
        task = get_task_manager(request).start(
            f"infra:{svc.id}",
            "pull",
            lambda: services.compose_action_streaming(svc, "pull"),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return task.to_dict(tail=1)


@router.post("/{service_id}/doctor")
def infra_doctor(request: Request, service_id: str):
    svc, instance_id = _resolve_infra_with_instance(service_id)
    if not svc.has_doctor:
        raise HTTPException(status_code=400, detail="service does not support doctor")
    try:
        task = get_task_manager(request).start(
            f"infra:{svc.id}",
            "doctor",
            lambda: services.doctor_streaming(svc, instance_id),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return task.to_dict(tail=1)


@router.get("/{service_id}/logs/stream")
def infra_logs_stream(service_id: str):
    svc, instance_id = _resolve_infra_with_instance(service_id)
    if svc.service_type == "compose":
        gen = services.compose_logs(svc)
    elif svc.service_type == "standalone":
        gen = services.standalone_logs(svc, instance_id or svc.bound_instance_ids[0])
    else:
        raise HTTPException(status_code=400, detail="embedded service has no log stream")
    return sse(gen)


@router.post("/{service_id}/login/qrcode")
def infra_login_qrcode(service_id: str):
    svc = _resolve_infra(service_id)
    if not svc.has_login:
        raise HTTPException(status_code=400, detail="service does not support login")
    res = operations.shared_service_xhs_login_qrcode()
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res.get("error") or "failed to get qrcode")
    return res


@router.post("/{service_id}/login/check")
def infra_login_check(service_id: str):
    svc, instance_id = _resolve_infra_with_instance(service_id)
    if not svc.has_login:
        raise HTTPException(status_code=400, detail="service does not support login check")
    if svc.service_type == "standalone" and svc.extra.get("login_type") == "webui_link":
        inst_id = instance_id or (svc.bound_instance_ids[0] if svc.bound_instance_ids else "")
        if not inst_id:
            raise HTTPException(status_code=400, detail="standalone service needs instance_id")
        res = services.standalone_webui_login_status(
            svc,
            inst_id,
        )
    else:
        res = operations.shared_service_xhs_check_login()
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res.get("error") or "failed to check login")
    services.update_login_cache(svc.id, "logged_in" if res.get("logged_in") else "logged_out")
    return res


@router.post("/{service_id}/login/token")
def infra_login_token(
    request: Request,
    response: Response,
    service_id: str,
):
    _require_loopback_request(request)
    svc, instance_id = _resolve_infra_with_instance(service_id)
    if (
        svc.service_type != "standalone"
        or svc.extra.get("login_type") != "webui_link"
    ):
        raise HTTPException(
            status_code=400,
            detail="service does not support WebUI token lookup",
        )
    inst_id = instance_id or (svc.bound_instance_ids[0] if svc.bound_instance_ids else "")
    if not inst_id:
        raise HTTPException(status_code=400, detail="standalone service needs instance_id")
    result = services.standalone_webui_token(svc, inst_id)
    if not result.get("ok"):
        raise HTTPException(
            status_code=409,
            detail=result.get("error") or "failed to get WebUI token",
        )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return result


def _resolve_infra(service_id: str) -> services.ServiceDef:
    sid = service_id.split(":")[0]
    svc = services.find_service(sid)
    if svc is None:
        raise HTTPException(status_code=404, detail=f"unknown service: {service_id}")
    return svc


def _resolve_infra_with_instance(service_id: str) -> tuple[services.ServiceDef, Optional[str]]:
    parts = service_id.split(":", 1)
    svc = services.find_service(parts[0])
    if svc is None:
        raise HTTPException(status_code=404, detail=f"unknown service: {service_id}")
    instance_id = parts[1] if len(parts) > 1 else None
    return svc, instance_id


def _infra_action(service_id: str, verb: str):
    svc, instance_id = _resolve_infra_with_instance(service_id)
    if verb not in svc.actions:
        raise HTTPException(status_code=400, detail=f"service {svc.id} does not support action: {verb}")
    if svc.service_type == "compose":
        res = services.compose_action(svc, verb)
    elif svc.service_type == "standalone":
        inst_id = instance_id or (svc.bound_instance_ids[0] if svc.bound_instance_ids else "")
        if not inst_id:
            raise HTTPException(status_code=400, detail="standalone service needs instance_id")
        res = services.standalone_action(svc, inst_id, verb)
    else:
        raise HTTPException(status_code=400, detail="embedded service has no lifecycle action")
    services.invalidate_status_cache()
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or res.get("stderr") or "action failed")
    return res
