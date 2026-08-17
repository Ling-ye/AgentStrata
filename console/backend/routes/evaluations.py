from __future__ import annotations

import json
from typing import Annotated, Any, Iterator, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from chatcopilot.evals.service import EvaluationServiceError
from console.backend.routes.common import (
    get_evaluation_client,
    raise_evaluation_service_error,
)

KEEPALIVE = "\x00"

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
            raise ValueError(f"{self.preset} preset rejects overrides: {', '.join(supplied)}")
        missing = [key for key, value in overrides.items() if value is None]
        if self.preset == "custom" and missing:
            raise ValueError(f"custom preset requires: {', '.join(missing)}")
        return self


class SuiteEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["suite"]
    bot_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    case_ids: list[str] = Field(default_factory=list)
    preset: Literal["quick", "full", "security", "qq-live", "custom"] = "custom"
    repetitions: int = Field(default=1, ge=1, le=10)
    max_wall_seconds: int = Field(default=0, ge=0, le=21600)
    seed: int = 0
    options: dict[str, Any] = Field(default_factory=dict)
    confirm_external_write: StrictBool = False
    dry_run: StrictBool = False
    llm_judge: StrictBool = False

    @model_validator(mode="after")
    def validate_manual_selection(self) -> SuiteEvaluationRequest:
        if self.preset == "custom" and not self.case_ids:
            raise ValueError("custom preset requires case_ids")
        if self.preset != "custom" and self.case_ids:
            raise ValueError("case_ids are only accepted with preset=custom")
        requires_external_write = self.preset == "qq-live" or (
            self.suite_id == "agentstrata-capabilities-v1" and self.preset == "full"
        )
        if requires_external_write and not self.confirm_external_write:
            raise ValueError(f"{self.preset} preset requires confirm_external_write=true")
        return self


EvaluationCreateRequest = Annotated[
    ComparisonEvaluationRequest | SuiteEvaluationRequest,
    Field(discriminator="kind"),
]


@router.get("/profiles")
def list_profiles(request: Request):
    try:
        return get_evaluation_client(request).list_profiles()
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.post("/evaluations")
def start_evaluation(
    request: Request,
    body: EvaluationCreateRequest,
):
    data = body.model_dump(exclude_none=True)
    try:
        return get_evaluation_client(request).start(
            bot_id=str(data["bot_id"]).strip(),
            request=data,
        )
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.get("/evaluations")
def list_evaluations(
    request: Request,
    kind: Literal["comparison", "suite"] | None = Query(default=None),
    bot_id: str | None = Query(default=None),
    target: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    try:
        return get_evaluation_client(request).list(
            kind=kind,
            bot_id=bot_id,
            target=target,
            status=status,
        )
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(request: Request, evaluation_id: str):
    try:
        return get_evaluation_client(request).get(evaluation_id)
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.get("/evaluations/{evaluation_id}/cases/{case_ref:path}")
def get_evaluation_case(
    request: Request,
    evaluation_id: str,
    case_ref: str,
):
    try:
        return get_evaluation_client(request).case_detail(
            evaluation_id,
            case_ref,
        )
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.get("/evaluations/{evaluation_id}/stream")
def stream_evaluation(
    request: Request,
    evaluation_id: str,
):
    client = get_evaluation_client(request)
    try:
        client.get(evaluation_id, include_result=False)
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)

    def events() -> Iterator[bytes]:
        try:
            for payload in client.follow(evaluation_id):
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
        except EvaluationServiceError as exc:
            encoded = json.dumps(
                {
                    "code": exc.code,
                    "message": exc.message,
                },
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {encoded}\n\n".encode()
        yield b"event: end\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/evaluations/{evaluation_id}/cancel")
def cancel_evaluation(request: Request, evaluation_id: str):
    try:
        return get_evaluation_client(request).cancel(evaluation_id)
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.post("/evaluations/{evaluation_id}/rerun")
def rerun_evaluation(request: Request, evaluation_id: str):
    try:
        return get_evaluation_client(request).clone(evaluation_id)
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)


@router.delete("/evaluations/{evaluation_id}")
def delete_evaluation(request: Request, evaluation_id: str):
    try:
        get_evaluation_client(request).delete(evaluation_id)
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)
    return {"ok": True}


@router.get("/evaluations/{evaluation_id}/export/{kind}")
def export_evaluation(
    request: Request,
    evaluation_id: str,
    kind: Literal["json", "markdown"],
):
    try:
        report = get_evaluation_client(request).report_stream(
            evaluation_id,
            kind,
        )
    except EvaluationServiceError as exc:
        raise_evaluation_service_error(exc)
    return StreamingResponse(
        report.chunks,
        media_type=report.media_type,
        headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
    )
