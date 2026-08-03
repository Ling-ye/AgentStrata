from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from console.backend.routes.common import get_task_manager
from console.backend.sse import sse

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
def list_tasks(request: Request):
    return get_task_manager(request).list()


@router.get("/{task_id}")
def get_task(request: Request, task_id: str, tail: Optional[int] = None):
    task = get_task_manager(request).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task.to_dict(tail=tail)


@router.get("/{task_id}/stream")
def stream_task(request: Request, task_id: str):
    task = get_task_manager(request).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return sse(task.follow())
