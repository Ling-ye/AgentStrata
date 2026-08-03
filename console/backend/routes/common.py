from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from console.backend.tasks import TaskManager
from console.control.discovery import find_instance
from console.control.evaluations import EvaluationManager
from console.control.instances import BotInstance


def get_task_manager(request: Request) -> TaskManager:
    manager = getattr(request.app.state, "tasks", None)
    if manager is None:
        raise HTTPException(status_code=500, detail="task manager is not initialized")
    return manager


def get_evaluation_manager(request: Request) -> EvaluationManager:
    manager = getattr(request.app.state, "evaluations", None)
    if manager is None:
        raise HTTPException(
            status_code=500,
            detail="evaluation manager is not initialized",
        )
    return manager


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
