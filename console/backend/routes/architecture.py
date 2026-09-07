from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Callable
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from chatcopilot.gateway.state_store import GatewayStateError
from chatcopilot.gateway.observation_queries import RunFilter
from console.backend.routes.common import get_instance
from console.control import gateway_observability

router = APIRouter(prefix="/api/bots", tags=["observability"])


def _read(response: Response, operation: Callable[[], Any]):
    response.headers["Cache-Control"] = "no-store"
    try:
        result = operation()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_observation_request", "message": "观测查询参数或实例配置无效"},
                            headers={"Cache-Control": "no-store"}) from exc
    except (OSError, RuntimeError, sqlite3.Error, GatewayStateError) as exc:
        raise HTTPException(status_code=409, detail={
            "code": "gateway_observation_unavailable", "message": "运行观测暂不可用，请检查实例状态及采集状态。",
        }, headers={"Cache-Control": "no-store"}) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Observation record not found", headers={"Cache-Control": "no-store"})
    return result


def run_filters(
    since: float | None = None, until: float | None = None, state: str = "", config_id: str = "",
    backend: str = "", model: str = "", component: str = "", error_code: str = "", search: str = "",
    min_ms: float | None = None, page: int = Query(1, ge=1, le=100000), limit: int = Query(50, ge=1, le=100),
) -> RunFilter:
    return RunFilter(since, until, state, config_id, backend, model, component, error_code, search, min_ms, page, limit)


Filters = Annotated[RunFilter, Depends(run_filters)]


@router.get("/{instance_id}/inspection")
def inspection(instance_id: str, response: Response, run_id: str | None = None, event_seq: int | None = None):
    return _read(response, lambda: gateway_observability.inspection(get_instance(instance_id), run_id=run_id, event_seq=event_seq))


@router.get("/{instance_id}/gateway-observation")
def gateway_snapshot(instance_id: str, response: Response, filters: Filters = RunFilter()):
    return _read(response, lambda: gateway_observability.snapshot(get_instance(instance_id), filters=filters))


@router.get("/{instance_id}/gateway-observation/metrics")
def gateway_metrics(instance_id: str, response: Response, filters: Filters = RunFilter()):
    return _read(response, lambda: gateway_observability.metrics(get_instance(instance_id), filters))


@router.get("/{instance_id}/gateway-observation/runs/{run_id}")
def gateway_run_snapshot(instance_id: str, run_id: str, response: Response):
    return _read(response, lambda: gateway_observability.snapshot(get_instance(instance_id), run_id))


@router.get("/{instance_id}/gateway-observation/runs/{run_id}/events")
def gateway_run_events(instance_id: str, run_id: str, response: Response,
                       after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
    return _read(response, lambda: gateway_observability.event_page(get_instance(instance_id), run_id, after, limit))


@router.get("/{instance_id}/gateway-observation/runs/{run_id}/details/{body_id}")
def gateway_run_body(instance_id: str, run_id: str, body_id: str, response: Response):
    return _read(response, lambda: gateway_observability.body(get_instance(instance_id), run_id, body_id))
