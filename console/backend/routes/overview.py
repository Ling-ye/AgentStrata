from __future__ import annotations

from fastapi import APIRouter, Request

from console.backend.routes.common import get_task_manager
from console.control import health
from console.control.discovery import discover_instances

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview")
def console_overview(request: Request):
    return health.overview(discover_instances(), get_task_manager(request))
