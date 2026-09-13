"""Optional Harness BFF. Normal Evaluation routes do not depend on this module."""

from __future__ import annotations

import ipaddress
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from chatcopilot.evals.service import EvaluationServiceError
from chatcopilot.gateway.state_store import GatewayStateError
from chatcopilot.harness.models import RepairFeedback
from console.control.discovery import repo_root

router = APIRouter(prefix="/api/harness", tags=["harness"])


class CreateRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: Literal["evaluation", "robot_task"] = "evaluation"
    case_instance_id: str = ""
    bot_id: str = ""
    run_id: str = ""
    feedback: RepairFeedback | None = None
    request_id: str
    model: str = Field(min_length=1)
    review_and_commit: StrictBool = False
    reasoning_effort: str = "medium"
    max_attempts: int = Field(default=3, ge=1)
    timeout_seconds: int = Field(default=7200, ge=1)

    @model_validator(mode="after")
    def selected_source(self):
        if self.source_kind == "evaluation":
            if self.feedback and self.feedback.expected_behavior.strip():
                raise ValueError("测评 Case 只能补充修复线索，不能覆盖原参考答案")
            valid = (
                self.case_instance_id
                and not (self.bot_id or self.run_id)
            )
        else:
            valid = (
                self.bot_id
                and self.run_id
                and not self.case_instance_id
            )
        if not valid:
            raise ValueError("必须选择一种完整的修复来源")
        return self


class LoadSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["evaluation", "robot_task"]
    source_id: str = Field(min_length=1, max_length=256)
    bot_id: str = ""


def _controller(request: Request):
    from chatcopilot.harness.api import HarnessController

    configured = getattr(request.app.state, "harness", None)
    if configured is not None:
        return configured
    from chatcopilot.harness.gateway_adapter import task_source
    from chatcopilot.harness.models import HarnessError
    from console.backend.routes.common import get_instance
    from console.control.gateway_observability import reader

    def task_reader(bot_id, run_id):
        instance = get_instance(bot_id)
        if instance.runtime_kind != "gateway":
            raise HarnessError("unsupported_source", "此实例不提供 Gateway 任务观测")
        return task_source(reader(instance), instance.instance_id, run_id)

    return HarnessController(repo_root(), task_reader=task_reader)


def _mutation_access(request: Request) -> None:
    # Repository writes need an operator-local request; ordinary Console reads
    # keep their existing access behavior. Headers cannot manufacture locality.
    try:
        local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise HTTPException(403, "修复操作仅接受本机请求；远程访问请使用本机 SSH 隧道")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "修复操作要求同源请求")


def _call(function):
    from chatcopilot.harness.models import HarnessError

    try:
        return function()
    except HarnessError as exc:
        raise HTTPException(
            404 if exc.code == "not_found" else 409, {"code": exc.code, "message": str(exc)}
        ) from exc
    except EvaluationServiceError as exc:
        raise HTTPException(404 if exc.code == "not_found" else 503,
                            {"code": exc.code, "message": "Case 实例或所属测评不存在，可能已被删除" if exc.code == "not_found" else exc.message}) from exc
    except (sqlite3.Error, GatewayStateError) as exc:
        raise HTTPException(409, "任务观测或修复数据库暂不可用") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/tasks")
def create(request: Request, body: CreateRepair):
    from chatcopilot.harness.models import RepairOptions

    _mutation_access(request)
    options = _call(
        lambda: RepairOptions(
            body.model, body.reasoning_effort, body.max_attempts, body.timeout_seconds
        )
    )
    if body.source_kind == "robot_task":
        return _call(
            lambda: _controller(request).start_task(
                body.bot_id,
                body.run_id,
                options,
                request_id=body.request_id,
                review_and_commit=body.review_and_commit,
                feedback=body.feedback,
            )
        )
    return _call(
        lambda: _controller(request).start_case_instance(
            body.case_instance_id,
            options,
            feedback=body.feedback,
            request_id=body.request_id,
            review_and_commit=body.review_and_commit,
        )
    )


@router.post("/sources/load")
def load_source(request: Request, body: LoadSource):
    return _call(lambda: _controller(request).load_source(body.kind, body.source_id, body.bot_id))


@router.get("/tasks")
def history(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query("", max_length=256),
    status: str = "",
):
    return _call(
        lambda: _controller(request).list(page=page, limit=limit, search=search, status=status)
    )


@router.get("/tasks/{task_id}/evidence")
def evidence(request: Request, task_id: str):
    return _call(lambda: _controller(request).evidence(task_id))


@router.get("/tasks/{task_id}/traces")
def traces(request: Request, task_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: _controller(request).trace_records(task_id))


@router.get("/tasks/{task_id}/traces/{ref}")
def trace_record(request: Request, task_id: str, ref: str, response: Response, after: int = Query(0, ge=0)):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: _controller(request).trace_record(task_id, ref, after=after))


@router.get("/tasks/{task_id}/traces/{ref}/steps/{span_id}")
def trace_step(request: Request, task_id: str, ref: str, span_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: _controller(request).trace_record(task_id, ref, span_id=span_id))


@router.get("/tasks/{task_id}/reproducer")
def reproducer(request: Request, task_id: str):
    content = _call(lambda: _controller(request).reproducer(task_id))
    return Response(
        content,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="test_reproduction.py"'},
    )


@router.get("/tasks/{task_id}")
def get(request: Request, task_id: str):
    return _call(lambda: _controller(request).get(task_id))


@router.post("/tasks/{task_id}/cancel")
def cancel(request: Request, task_id: str):
    _mutation_access(request)
    return _call(lambda: _controller(request).cancel(task_id))


@router.post("/tasks/{task_id}/resume")
def resume(request: Request, task_id: str):
    _mutation_access(request)
    return _call(lambda: _controller(request).resume(task_id))


@router.get("/tasks/{task_id}/attempts/{number}/patch")
def patch(request: Request, task_id: str, number: int):
    content = _call(lambda: _controller(request).patch(task_id, number))
    return Response(
        content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="candidate-{number}.patch"'},
    )
