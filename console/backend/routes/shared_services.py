from __future__ import annotations

from fastapi import APIRouter, HTTPException

from console.control import operations

router = APIRouter(prefix="/api/shared-services", tags=["shared-services"])


@router.post("/xhs/start")
def shared_service_xhs_start():
    res = operations.shared_service_xhs_start()
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or res.get("stderr") or "failed to start xhs MCP")
    return res


@router.post("/xhs/login-qrcode")
def shared_service_xhs_login_qrcode():
    res = operations.shared_service_xhs_login_qrcode()
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res.get("error") or "failed to get xhs qrcode")
    return res


@router.post("/xhs/check-login")
def shared_service_xhs_check_login():
    res = operations.shared_service_xhs_check_login()
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res.get("error") or "failed to check xhs login")
    return res
