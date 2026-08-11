from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from chatcopilot.evals.service import EvaluationServiceError
from console.backend.routes.common import (
    get_evaluation_client,
    raise_evaluation_service_error,
    start_named_task,
)

router = APIRouter(prefix="/api/evals", tags=["evals"])


@router.get("/suites")
def list_suites(
    request: Request,
    bot_id: str | None = Query(default=None),
):
    try:
        return get_evaluation_client(request).list_suites(bot_id=bot_id)
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.get("/health")
def evaluation_health(request: Request):
    try:
        return get_evaluation_client(request).health()
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.post("/suites/{suite_id}/prepare")
def prepare_suite(
    request: Request,
    suite_id: str,
    bot_id: str | None = Query(default=None),
):
    task_key = f"eval-prepare:{bot_id or 'global'}:{suite_id}"
    try:
        client = get_evaluation_client(request)
        client.health()
        return start_named_task(
            request,
            task_key,
            f"eval-prepare:{suite_id}",
            lambda: client.prepare_suite(suite_id, bot_id=bot_id),
        )
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/suites/{suite_id}/cases")
def list_cases(
    request: Request,
    suite_id: str,
    bot_id: str | None = Query(default=None),
):
    try:
        return {
            "suite_id": suite_id,
            "cases": get_evaluation_client(request).list_cases(
                suite_id,
                bot_id=bot_id,
            ),
        }
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.get("/suites/{suite_id}/cases/{case_id:path}")
def get_case(
    request: Request,
    suite_id: str,
    case_id: str,
    bot_id: str | None = Query(default=None),
):
    try:
        return get_evaluation_client(request).get_case(
            suite_id,
            case_id,
            bot_id=bot_id,
        )
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.get("/cases/coverage")
def list_case_coverage(
    request: Request,
    bot_id: str | None = Query(default=None),
):
    try:
        return get_evaluation_client(request).coverage(bot_id=bot_id)
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)
