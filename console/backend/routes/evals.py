from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from console.backend.routes.common import (
    get_evaluation_manager,
    get_instance,
    start_named_task,
)
from console.control import evals as eval_control

router = APIRouter(prefix="/api/evals", tags=["evals"])


def _optional_instance(bot_id: str | None):
    return get_instance(bot_id) if bot_id else None


@router.get("/suites")
def list_suites(bot_id: str | None = Query(default=None)):
    return eval_control.list_suite_descriptors(_optional_instance(bot_id))


@router.post("/suites/{suite_id}/prepare")
def prepare_suite(
    request: Request,
    suite_id: str,
    bot_id: str | None = Query(default=None),
):
    instance = _optional_instance(bot_id)
    task_key = f"eval-prepare:{bot_id or 'global'}:{suite_id}"
    try:
        return start_named_task(
            request,
            task_key,
            f"eval-prepare:{suite_id}",
            lambda: eval_control.stream_prepare_suite(suite_id, instance),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/suites/{suite_id}/cases")
def list_cases(suite_id: str, bot_id: str | None = Query(default=None)):
    try:
        return {
            "suite_id": suite_id,
            "cases": eval_control.list_case_summaries(suite_id, _optional_instance(bot_id)),
        }
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/suites/{suite_id}/cases/{case_id:path}")
def get_case(suite_id: str, case_id: str, bot_id: str | None = Query(default=None)):
    try:
        return eval_control.get_case_descriptor(suite_id, case_id, _optional_instance(bot_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/coverage")
def list_case_coverage(
    request: Request,
    bot_id: str | None = Query(default=None),
):
    return get_evaluation_manager(request).coverage(bot_id=bot_id)
