from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from chatcopilot.evals.service import (
    EvaluationServiceClient,
    EvaluationServiceError,
)
from console.backend.tasks import TaskManager
from console.control.discovery import find_instance
from console.control.instances import BotInstance


def get_task_manager(request: Request) -> TaskManager:
    manager = getattr(request.app.state, "tasks", None)
    if manager is None:
        raise HTTPException(status_code=500, detail="task manager is not initialized")
    return manager


def get_evaluation_client(request: Request) -> EvaluationServiceClient:
    client = getattr(request.app.state, "evaluations", None)
    if client is None:
        raise HTTPException(
            status_code=500,
            detail="evaluation service client is not initialized",
        )
    return client


def raise_evaluation_service_error(exc: EvaluationServiceError) -> None:
    status_by_code = {
        "evaluation_blocked": 422,
        "preflight_failed": 422,
        "configuration_invalid": 422,
        "not_found": 404,
        "conflict": 409,
        "invalid_request": 400,
        "service_unavailable": 503,
        "invalid_response": 502,
        "internal_error": 502,
    }
    status_code = status_by_code.get(exc.code, 502)
    detail: object
    if exc.code in {"evaluation_blocked", "preflight_failed", "configuration_invalid"}:
        detail = {
            "code": exc.code,
            "message": exc.message,
            "checks": exc.checks,
        }
    else:
        detail = exc.message
    raise HTTPException(status_code=status_code, detail=detail) from exc


def get_instance(instance_id: str) -> BotInstance:
    inst = find_instance(instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail=f"instance not found: {instance_id}")
    return inst


def start_task(request: Request, inst: BotInstance, kind: str, factory: Any):
    return start_named_task(request, inst.instance_id, kind, factory)


def start_named_task(request: Request, task_key: str, kind: str, factory: Any):
    try:
        task = get_task_manager(request).start(task_key, kind, factory)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return task.to_dict(tail=1)
