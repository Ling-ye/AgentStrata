from __future__ import annotations

import json
from typing import Annotated, Any, Iterator, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from console.backend.routes.common import get_evaluation_manager, get_instance
from console.control import evals as eval_control
from console.control.evaluations import EvaluationBlocked
from console.control.operations import KEEPALIVE

router = APIRouter(prefix="/api/evals", tags=["evaluations"])


class ComparisonEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["comparison"]
    bot_id: str = Field(min_length=1)
    profile_id: str = Field(default="agent-comparison-mvp", min_length=1)
    preset: Literal["quick", "standard", "custom"] = "quick"
    target_ids: list[Literal["codex", "native"]] | None = Field(
        default=None,
        min_length=1,
    )
    case_refs: list[str] | None = Field(default=None, min_length=1)
    repetitions: int | None = Field(default=None, ge=1, le=10)
    max_wall_seconds: int | None = Field(default=None, ge=30, le=21600)
    seed: int | None = None

    @model_validator(mode="after")
    def validate_preset_fields(self) -> ComparisonEvaluationRequest:
        overrides = {
            "target_ids": self.target_ids,
            "case_refs": self.case_refs,
            "repetitions": self.repetitions,
            "max_wall_seconds": self.max_wall_seconds,
            "seed": self.seed,
        }
        supplied = [key for key, value in overrides.items() if value is not None]
        if self.preset != "custom" and supplied:
            raise ValueError(
                f"{self.preset} preset rejects overrides: {', '.join(supplied)}"
            )
        missing = [key for key, value in overrides.items() if value is None]
        if self.preset == "custom" and missing:
            raise ValueError(
                f"custom preset requires: {', '.join(missing)}"
            )
        return self


class SuiteEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["suite"]
    bot_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    case_ids: list[str] = Field(min_length=1)
    dry_run: bool = False
    llm_judge: bool = False


EvaluationCreateRequest = Annotated[
    ComparisonEvaluationRequest | SuiteEvaluationRequest,
    Field(discriminator="kind"),
]


def _raise_control_error(exc: Exception) -> None:
    if isinstance(exc, EvaluationBlocked):
        raise HTTPException(status_code=422, detail=exc.payload) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (ValueError, OSError)):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/profiles")
def list_profiles():
    return eval_control.list_profile_descriptors()


@router.post("/evaluations")
def start_evaluation(
    request: Request,
    body: EvaluationCreateRequest,
):
    data = body.model_dump(exclude_none=True)
    instance = get_instance(str(data["bot_id"]).strip())
    try:
        return get_evaluation_manager(request).start(
            instance=instance,
            request=data,
        )
    except (EvaluationBlocked, RuntimeError, ValueError, OSError) as exc:
        _raise_control_error(exc)


@router.get("/evaluations")
def list_evaluations(
    request: Request,
    kind: Literal["comparison", "suite"] | None = Query(default=None),
    bot_id: str | None = Query(default=None),
    target: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    return get_evaluation_manager(request).list(
        kind=kind,
        bot_id=bot_id,
        target=target,
        status=status,
    )


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(request: Request, evaluation_id: str):
    try:
        return get_evaluation_manager(request).get(evaluation_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="evaluation not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/evaluations/{evaluation_id}/cases/{case_ref:path}")
def get_evaluation_case(
    request: Request,
    evaluation_id: str,
    case_ref: str,
):
    try:
        return get_evaluation_manager(request).case_detail(
            evaluation_id,
            case_ref,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="evaluation case result not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/evaluations/{evaluation_id}/stream")
def stream_evaluation(
    request: Request,
    evaluation_id: str,
):
    manager = get_evaluation_manager(request)
    try:
        manager.get(evaluation_id, include_result=False)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="evaluation not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def events() -> Iterator[bytes]:
        for payload in manager.follow(evaluation_id):
            if payload == KEEPALIVE:
                yield b": ping\n\n"
                continue
            data: dict[str, Any]
            if isinstance(payload, dict):
                data = payload
            else:
                data = {"event": "log", "message": str(payload)}
            encoded = json.dumps(data, ensure_ascii=False)
            yield f"data: {encoded}\n\n".encode()
        yield b"event: end\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/evaluations/{evaluation_id}/cancel")
def cancel_evaluation(request: Request, evaluation_id: str):
    try:
        return get_evaluation_manager(request).cancel(evaluation_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="evaluation not found",
        ) from exc
    except (RuntimeError, ValueError) as exc:
        _raise_control_error(exc)


@router.post("/evaluations/{evaluation_id}/rerun")
def rerun_evaluation(request: Request, evaluation_id: str):
    manager = get_evaluation_manager(request)
    try:
        current = manager.get(evaluation_id, include_result=False)
        instance = get_instance(str(current["bot_id"]))
        return manager.clone(evaluation_id, instance=instance)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="evaluation not found",
        ) from exc
    except (EvaluationBlocked, RuntimeError, ValueError, OSError) as exc:
        _raise_control_error(exc)


@router.delete("/evaluations/{evaluation_id}")
def delete_evaluation(request: Request, evaluation_id: str):
    try:
        get_evaluation_manager(request).delete(evaluation_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="evaluation not found",
        ) from exc
    except (RuntimeError, ValueError) as exc:
        _raise_control_error(exc)
    return {"ok": True}


@router.get("/evaluations/{evaluation_id}/export/{kind}")
def export_evaluation(
    request: Request,
    evaluation_id: str,
    kind: Literal["json", "markdown"],
):
    try:
        path = get_evaluation_manager(request).report_path(
            evaluation_id,
            kind,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="evaluation report not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    media_type = "application/json" if kind == "json" else "text/markdown"
    return FileResponse(path, media_type=media_type, filename=path.name)
