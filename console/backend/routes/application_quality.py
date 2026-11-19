"""Application-owned views of the repository Eval and Harness services."""

from typing import Literal
import re
import sqlite3
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from console.backend.routes import evaluations, evals, harness
from console.backend.routes.application_records import repository_instance
from console.backend.routes.applications import PrivateRoute, access, call, service
from console.backend.routes.common import get_evaluation_client
from console.control import project_evaluation, project_repair
from chatcopilot.evals.service import EvaluationServiceError
from chatcopilot.harness.models import HarnessError

router = APIRouter(
    prefix="/api/applications/{application_id}",
    tags=["application quality"],
    dependencies=[Depends(access)],
    route_class=PrivateRoute,
)


def project_call(operation):
    try:
        return operation()
    except project_evaluation.ProjectIssue as exc:
        raise HTTPException(400, str(exc)) from None
    except KeyError:
        raise HTTPException(404, "当前应用没有该记录") from None
    except (HarnessError, EvaluationServiceError) as exc:
        raise HTTPException(
            404 if exc.code == "not_found" else 409, "无法核对此应用的来源或任务"
        ) from None
    except (sqlite3.IntegrityError, PermissionError, ValueError):
        raise HTTPException(409, "请核对当前应用的开发基线、权限和进行中的操作") from None
    except (OSError, RuntimeError, sqlite3.Error):
        raise HTTPException(503, "本应用的开发服务或证据暂不可用") from None


def same(actual, expected):
    if actual != expected:
        raise HTTPException(404, "当前应用没有此记录")


def evaluation_owner(request: Request, application_id: str):
    apps = service(request)
    row = call(lambda: apps.require(application_id))
    if row["source"] == "repository":
        return repository_instance(request, application_id)
    try:
        client, bot = project_evaluation.connection(
            apps, row, verify_source=request.method != "GET"
        )
    except (ValueError, EvaluationServiceError, OSError) as exc:
        raise HTTPException(409, "本应用的测评服务尚未连接或已离线，请重新连接") from exc
    request.state.application_evaluation = client
    return bot


@router.get("/evaluation-service")
def service_status(request: Request, application_id: str):
    try:
        return {"ready": True, "bot_id": evaluation_owner(request, application_id)}
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return {"ready": False, "message": exc.detail}


@router.post("/evaluation-service")
def connect_evaluation(request: Request, application_id: str):
    apps = service(request)
    row = call(lambda: apps.require(application_id))
    if row["source"] != "studio":
        raise HTTPException(409, "请先接入当前应用的开发源码")
    try:
        with apps._operation_lock(row):
            project_evaluation.connect(apps, row)
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(409, "请核对应用源码基线与本机测评服务后重试") from exc
    return service_status(request, application_id)


class ProjectRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    kind: Literal["evaluation", "robot_task"]
    source_id: str = Field(min_length=1, max_length=256)
    digest: str = Field(pattern="^[0-9a-f]{64}$")
    feedback: str = Field(default="", max_length=8000)


@router.get("/project-repairs/source")
def project_source(
    request: Request,
    application_id: str,
    kind: Literal["evaluation", "robot_task"],
    source_id: str = Query(min_length=1, max_length=256),
):
    apps = service(request)
    row = call(lambda: apps.require(application_id))
    if row["source"] != "studio":
        raise HTTPException(409, "请先导入与此应用匹配的可验证开发源码")
    receipt = project_call(lambda: project_repair.source(apps, row, kind, source_id))
    return {
        key: receipt[key]
        for key in ("kind", "id", "digest", "evaluation_id", "case_ref", "outcome", "run_id")
        if key in receipt
    }


@router.post("/project-repairs")
def start_project_repair(request: Request, application_id: str, body: ProjectRepair):
    apps = service(request)
    row = call(lambda: apps.require(application_id))
    return project_call(
        lambda: project_repair.start(
            apps, row, body.id.hex, body.kind, body.source_id, body.digest, body.feedback.strip()
        )
    )


@router.get("/project-tasks/{task_id}/flow")
def project_flow(request: Request, application_id: str, task_id: str):
    apps = service(request)
    row = call(lambda: apps.require(application_id))
    host = project_call(lambda: project_repair.task_host(apps, row, task_id))
    flow = project_call(lambda: host.flow(task_id))
    task = project_call(lambda: host.get(task_id))
    origin = task["source"].get("repair_origin") or {}
    # Private projects expose host-owned stage facts, not raw worker output or local paths.
    return {
        "steps": [
            {k: s.get(k) for k in ("id", "title", "status", "phase", "started_at", "finished_at")}
            for s in flow["steps"]
        ],
        "goal": task["source"].get("goal", ""),
        "candidate_digest": (task.get("accepted_candidate") or {}).get("candidate_digest"),
        "origin": {
            key: origin[key] for key in ("id", "kind", "digest", "evaluation_id") if key in origin
        },
    }


@router.get("/project-tasks/{task_id}/commands")
def project_commands(
    request: Request,
    application_id: str,
    task_id: str,
    source_id: str = Query("", max_length=200),
    cursor: str = Query("", max_length=1000),
):
    apps = service(request)
    row = call(lambda: apps.require(application_id))
    host = project_call(lambda: project_repair.task_host(apps, row, task_id))
    records = project_call(lambda: host.commands(task_id, source_id=source_id, cursor=cursor))
    events = []
    for event in records["events"]:
        name = str(event.get("command", "")).split(maxsplit=1)[:1]
        program = name[0] if name and re.fullmatch(r"[a-zA-Z][\w.\-]*", name[0]) else "命令"
        events.append({"id": event["id"], "program": program, "exit_code": event.get("exit_code")})
    return {
        "sources": [{k: s.get(k) for k in ("id", "kind", "number")} for s in records["sources"]],
        "selected": (records.get("source") or {}).get("id"),
        "events": events,
        "next_cursor": records["next_cursor"],
        "has_more": records["has_more"],
    }


def check_evaluation(request: Request, evaluation_id: str, bot_id=Depends(evaluation_owner)):
    record = harness._call(
        lambda: get_evaluation_client(request).get(evaluation_id, include_result=False)
    )
    same(record.get("bot_id"), bot_id)


def check_case(request: Request, case_instance_id: str, bot_id=Depends(evaluation_owner)):
    case = harness._call(lambda: get_evaluation_client(request).case_instance(case_instance_id))
    same(case.get("case_instance_id"), case_instance_id)
    check_evaluation(request, case["evaluation_id"], bot_id)


def check_task(request: Request, task_id: str, bot_id=Depends(repository_instance)):
    task = harness._call(lambda: harness._controller(request).summary(task_id))
    same(task["source"].get("bot_id"), bot_id)
    if task["source"].get("kind") == "code_health":
        raise HTTPException(404, "项目治理任务属于平台管理")


def check_selected(bot_id: str, owner=Depends(repository_instance)):
    same(bot_id, owner)


def check_eval_selected(bot_id: str, owner=Depends(evaluation_owner)):
    same(bot_id, owner)


def scoped_routes(prefix, domain_router, endpoints, guard):
    """Register explicit typed operations with their existing response and error contracts."""
    for route in domain_router.routes:
        if route.endpoint in endpoints:
            router.add_api_route(
                prefix + route.path.removeprefix(domain_router.prefix),
                route.endpoint,
                methods=sorted(route.methods),
                dependencies=[Depends(guard)],
                name="application_" + route.name,
            )


scoped_routes(
    "/evals",
    evals.router,
    {
        evals.list_suites,
        evals.prepare_suite,
        evals.list_cases,
        evals.get_case,
        evals.list_case_coverage,
    },
    check_eval_selected,
)
scoped_routes("/evals", evaluations.router, {evaluations.list_profiles}, evaluation_owner)
scoped_routes(
    "/evals",
    evaluations.router,
    {
        evaluations.get_evaluation,
        evaluations.get_evaluation_case,
        evaluations.stream_evaluation,
        evaluations.cancel_evaluation,
        evaluations.rerun_evaluation,
        evaluations.delete_evaluation,
        evaluations.export_evaluation,
    },
    check_evaluation,
)
scoped_routes(
    "/evals", evaluations.router, {evaluations.case_trace, evaluations.case_trace_step}, check_case
)


@router.get("/evals/evaluations")
def list_evaluations(
    request: Request,
    owner=Depends(evaluation_owner),
    since: str = "",
    until: str = "",
    offset: int = Query(0, ge=0),
    limit: int | None = Query(None, ge=1, le=200),
):
    return harness._call(
        lambda: get_evaluation_client(request).list(
            bot_id=owner,
            offset=offset,
            **{k: v for k, v in (("since", since), ("until", until), ("limit", limit)) if v},
        )
    )


@router.post("/evals/evaluations")
def start_evaluation(
    request: Request, body: evaluations.EvaluationCreateRequest, owner=Depends(evaluation_owner)
):
    same(body.bot_id, owner)
    return evaluations.start_evaluation(request, body)


@router.get("/harness/tasks")
def tasks(
    request: Request,
    owner=Depends(repository_instance),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: str = "",
    search: str = "",
    kind: str = "repair",
):
    if kind not in {"repair", "application"}:
        raise HTTPException(400, "请选择应用修复或需求记录")
    return harness._call(
        lambda: harness._controller(request).list(
            bot_id=owner, page=page, limit=limit, status=status, search=search, kind=kind
        )
    )


def check_source(request, kind, source_id, bot_id, owner):
    if kind == "evaluation":
        check_case(request, source_id, owner)
    else:
        same(bot_id, owner)


@router.post("/harness/sources/load")
def load_source(request: Request, body: harness.LoadSource, owner=Depends(repository_instance)):
    check_source(request, body.kind, body.source_id, body.bot_id, owner)
    preview = harness.load_source(request, body)
    same(preview.get("bot_id"), owner)
    return preview


@router.post("/harness/tasks")
def start_repair(request: Request, body: harness.CreateRepair, owner=Depends(repository_instance)):
    check_source(
        request, body.source_kind, body.case_instance_id or body.run_id, body.bot_id, owner
    )
    return harness.start_repair_request(request, body, expected_bot_id=owner)


@router.get("/harness/applications")
def improvement_choices(request: Request, owner=Depends(repository_instance)):
    choices = harness.applications(request)
    return {
        "applications": [row for row in choices["applications"] if row["bot_id"] == owner],
        "activation_targets": [
            row for row in choices["activation_targets"] if row["target_id"] == "instance:" + owner
        ],
        "unavailable": [row for row in choices["unavailable"] if row["application"] == owner],
    }


@router.post("/harness/improvements")
def improve(request: Request, body: harness.CreateImprovement, owner=Depends(repository_instance)):
    same(body.bot_id, owner)
    return harness.create_improvement(request, body)


scoped_routes("/harness", harness.router, {harness.preview_improvement}, check_selected)
scoped_routes(
    "/harness",
    harness.router,
    {
        harness.get,
        harness.progress,
        harness.evidence,
        harness.repair_flow,
        harness.repair_step,
        harness.repair_commands,
        harness.traces,
        harness.trace_record,
        harness.trace_step,
        harness.reproducer,
        harness.comparison,
        harness.candidate_patch,
        harness.check_log,
        harness.cancel,
        harness.resume,
        harness.continue_task,
        harness.supply_image,
        harness.patch,
        harness.retry_delivery,
        harness.retry_cleanup,
    },
    check_task,
)


@router.get("/harness/tasks/{task_id}/activation", dependencies=[Depends(check_task)])
def activation(request: Request, task_id: str, owner=Depends(repository_instance)):
    record = harness.activation(request, task_id)
    same(record.get("target_id"), "instance:" + owner)
    return record


@router.post("/harness/tasks/{task_id}/activation/{action}", dependencies=[Depends(check_task)])
def activate(
    request: Request,
    task_id: str,
    body: harness.ActivationTarget,
    action: Literal["prepare", "activate", "rollback", "reconcile", "cancel"],
    owner=Depends(repository_instance),
):
    target = "instance:" + owner
    if body.target_id:
        same(body.target_id, target)
    try:
        activation(request, task_id, owner)
    except HTTPException as exc:
        if (
            action not in {"prepare", "activate"}
            or not isinstance(exc.detail, dict)
            or exc.detail.get("code") != "activation_missing"
        ):
            raise
    return harness.change_activation(
        request, task_id, action, body.model_copy(update={"target_id": target})
    )
