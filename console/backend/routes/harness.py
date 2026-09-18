"""Optional Harness BFF. Normal Evaluation routes do not depend on this module."""

from __future__ import annotations

import ipaddress
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from chatcopilot.evals.service import EvaluationServiceError
from chatcopilot.gateway.state_store import GatewayStateError
from chatcopilot.harness.models import RepairFeedback

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
    reasoning_effort: str = "xhigh"
    max_attempts: int = Field(default=3, ge=1)
    timeout_seconds: int = Field(default=3600, ge=1)

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


class TimeBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["time"]
    seconds: int = Field(strict=True, ge=1)


class FixedGroupsBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["fixed_groups"]
    count: int = Field(strict=True, ge=1)


class DiscoveredGroupsBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["discovered_groups"]
    count: int = Field(strict=True, ge=1)


class CreateCodeHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["all", "runtime", "console", "docs"] = "all"
    request_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = "xhigh"
    max_attempts: int = Field(default=3, ge=1)
    budget: TimeBudget | FixedGroupsBudget | DiscoveredGroupsBudget = Field(discriminator="mode")


def _controller(request: Request):
    configured = getattr(request.app.state, "harness", None)
    if configured is None:
        raise HTTPException(503, "Harness 控制入口未初始化，请检查 Console 启动日志")
    return configured


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
                feedback=body.feedback,
            )
        )
    return _call(
        lambda: _controller(request).start_case_instance(
            body.case_instance_id,
            options,
            feedback=body.feedback,
            request_id=body.request_id,
        )
    )


@router.post("/sources/load")
def load_source(request: Request, body: LoadSource):
    return _call(lambda: _controller(request).load_source(body.kind, body.source_id, body.bot_id))


@router.get("/code-health/config")
def code_health_config(request: Request):
    return _call(lambda: _controller(request).code_health_config())


@router.post("/code-health/tasks")
def create_code_health(request: Request, body: CreateCodeHealth):
    from chatcopilot.harness.models import CodeHealthOptions
    _mutation_access(request)
    return _call(lambda: _controller(request).start_code_health(
        body.scope, CodeHealthOptions(body.model, body.budget.model_dump(), body.reasoning_effort,
                                     body.max_attempts),
        request_id=body.request_id))


@router.get("/tasks/{task_id}/check-log")
def check_log(request: Request, task_id: str, reference: str):
    return _call(lambda: _controller(request).check_log(task_id, reference))


@router.get("/tasks")
def history(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query("", max_length=256),
    status: str = "",
    kind: Literal["", "repair", "code_health"] = "",
):
    return _call(
            lambda: _controller(request).list(page=page, limit=limit, search=search, status=status,
                                              **({"kind": kind} if kind else {}))
        )


@router.get("/tasks/{task_id}/evidence")
def evidence(request: Request, task_id: str):
    return _call(lambda: _controller(request).evidence(task_id))


@router.get("/tasks/{task_id}/traces")
def traces(request: Request, task_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: _controller(request).trace_records(task_id))


@router.get("/tasks/{task_id}/flow")
def repair_flow(request: Request, task_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: _controller(request).flow(task_id))


@router.get("/tasks/{task_id}/flow/steps/{step_id}")
def repair_step(request: Request, task_id: str, step_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: _controller(request).flow(task_id, step_id=step_id))


@router.get("/tasks/{task_id}/commands")
def repair_commands(request: Request, task_id: str, response: Response,
                    source_id: str = Query("", max_length=200), cursor: str = Query("", max_length=1000)):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: _controller(request).commands(task_id, source_id=source_id, cursor=cursor))


@router.get("/tasks/{task_id}/progress")
def progress(request: Request, task_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return _call(lambda: _controller(request).progress(task_id))
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise


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
def get(request: Request, task_id: str, response: Response, summary: bool = False):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: _controller(request).summary(task_id) if summary else _controller(request).get(task_id))


@router.post("/tasks/{task_id}/cancel")
def cancel(request: Request, task_id: str):
    _mutation_access(request)
    return _call(lambda: _controller(request).cancel(task_id))


@router.post("/tasks/{task_id}/resume")
def resume(request: Request, task_id: str):
    _mutation_access(request)
    return _call(lambda: _controller(request).resume(task_id))


@router.get("/tasks/{task_id}/candidate-patch")
def candidate_patch(request: Request, task_id: str):
    return Response(_call(lambda: _controller(request).candidate_patch(task_id)), media_type="text/x-diff",
                    headers={"Content-Disposition": 'attachment; filename="candidate.patch"'})


@router.get("/tasks/{task_id}/attempts/{number}/patch")
def patch(request: Request, task_id: str, number: int):
    content = _call(lambda: _controller(request).patch(task_id, number))
    return Response(
        content,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="candidate-{number}.patch"'},
    )


@router.post("/tasks/{task_id}/continue")
def continue_task(request: Request, task_id: str):
    _mutation_access(request)
    return _call(lambda: _controller(request).continue_task(task_id))


@router.post("/tasks/{task_id}/image")
async def supply_image(request: Request, task_id: str):
    from chatcopilot.core.image_content import HARD_IMAGE_INPUT_MAX_BYTES
    _mutation_access(request)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > HARD_IMAGE_INPUT_MAX_BYTES:
            raise HTTPException(413, "图片超过大小上限")
    return _call(lambda: _controller(request).supply_image(task_id, bytes(data)))


@router.post("/tasks/{task_id}/retry-delivery")
def retry_delivery(request: Request, task_id: str):
    _mutation_access(request)
    return _call(lambda: _controller(request).retry_delivery(task_id))


@router.post("/tasks/{task_id}/retry-cleanup")
def retry_cleanup(request: Request, task_id: str):
    _mutation_access(request)
    return _call(lambda: _controller(request).retry_cleanup(task_id))
