"""History-backed token forecasts for schema-v2 task records."""
from __future__ import annotations

from statistics import median
from typing import Any, Iterable, Mapping

from .task_projection import _USAGE_KEYS, normalize_usage

FORECAST_MIN_SAMPLES = 20
FORECAST_MAX_SAMPLES = 200
FORECAST_VERSION = "task-median-v1"


def median_usage(samples: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    normalized = [normalize_usage(sample) for sample in samples]
    if not normalized:
        return normalize_usage({})
    return {
        key: int(median([sample[key] for sample in normalized]))
        for key in (*_USAGE_KEYS, "input_tokens", "non_cached_input_tokens", "output_tokens")
    }


def forecast_llm_usage(
    history: Iterable[Mapping[str, Any]],
    *,
    model: str,
    context_kind: str,
    role: str,
    rough_input_tokens: int,
) -> dict[str, Any]:
    matching: list[dict[str, Any]] = []
    for task in history:
        for call in task.get("llm_calls") or []:
            if not isinstance(call, Mapping):
                continue
            if str(call.get("kind") or "llm") != "llm":
                continue
            call_role = str(call.get("role") or ("main" if int(call.get("depth") or 0) <= 0 else "subagent"))
            if (
                str(call.get("model") or "") == model
                and str(call.get("context_kind") or "") == context_kind
                and call_role == role
                and isinstance(call.get("usage"), Mapping)
            ):
                matching.append(dict(call))
                if len(matching) >= FORECAST_MAX_SAMPLES:
                    break
        if len(matching) >= FORECAST_MAX_SAMPLES:
            break

    sample_count = len(matching)
    estimate = normalize_usage({"prompt_tokens": rough_input_tokens})
    calibration_ratio = 1.0
    if sample_count >= FORECAST_MIN_SAMPLES:
        ratios = [
            normalize_usage(call.get("usage"))["prompt_tokens"]
            / max(1, int(call.get("raw_input_estimated_tokens") or call.get("input_estimated_tokens") or 0))
            for call in matching
            if int(call.get("raw_input_estimated_tokens") or call.get("input_estimated_tokens") or 0) > 0
        ]
        if len(ratios) >= FORECAST_MIN_SAMPLES:
            calibration_ratio = float(median(ratios[:FORECAST_MAX_SAMPLES]))
        historical = median_usage(call["usage"] for call in matching)
        calibrated_input = max(0, int(round(rough_input_tokens * calibration_ratio)))
        estimate = normalize_usage(
            {
                **historical,
                "prompt_tokens": calibrated_input,
                "total_tokens": calibrated_input + historical["completion_tokens"],
            }
        )
        status = "ready"
    else:
        status = "rough"
    return {
        "status": status,
        "sample_count": sample_count,
        "max_samples": FORECAST_MAX_SAMPLES,
        "min_samples": FORECAST_MIN_SAMPLES,
        "calibration_ratio": round(calibration_ratio, 4),
        "usage": estimate,
        "estimator_version": FORECAST_VERSION,
    }


def forecast_task_usage(
    history: Iterable[Mapping[str, Any]],
    *,
    model: str,
    context_kind: str,
) -> dict[str, Any]:
    samples: list[Mapping[str, Any]] = []
    for task in history:
        if task.get("status") not in {"succeeded", "failed"}:
            continue
        primary_model = str(
            task.get("primary_model")
            or next(
                (
                    call.get("model")
                    for call in task.get("llm_calls") or []
                    if isinstance(call, Mapping) and call.get("model")
                ),
                "",
            )
        )
        primary_context = str(
            task.get("context_kind")
            or next(
                (
                    call.get("context_kind")
                    for call in task.get("llm_calls") or []
                    if isinstance(call, Mapping) and call.get("context_kind")
                ),
                "",
            )
        )
        if primary_model != model or primary_context != context_kind:
            continue
        usage = task.get("usage_totals")
        if isinstance(usage, Mapping):
            samples.append(usage)
            if len(samples) >= FORECAST_MAX_SAMPLES:
                break
    count = len(samples)
    return {
        "status": "ready" if count >= FORECAST_MIN_SAMPLES else "insufficient",
        "model": model,
        "context_kind": context_kind,
        "sample_count": count,
        "max_samples": FORECAST_MAX_SAMPLES,
        "min_samples": FORECAST_MIN_SAMPLES,
        "estimator_version": FORECAST_VERSION,
        "baseline": median_usage(samples) if count >= FORECAST_MIN_SAMPLES else None,
    }


__all__ = [
    "FORECAST_MAX_SAMPLES",
    "FORECAST_MIN_SAMPLES",
    "FORECAST_VERSION",
    "forecast_llm_usage",
    "forecast_task_usage",
    "median_usage",
    "normalize_usage",
]
