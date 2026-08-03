"""History-backed token forecasts for schema-v2 task records."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

FORECAST_MIN_SAMPLES = 20
FORECAST_MAX_SAMPLES = 200
FORECAST_VERSION = "task-median-v1"

_USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    source = usage or {}
    normalized: dict[str, int] = {}
    for key in _USAGE_KEYS:
        value = source.get(key, 0)
        try:
            normalized[key] = max(0, int(float(value)))
        except (TypeError, ValueError):
            normalized[key] = 0
    prompt = normalized["prompt_tokens"]
    cached = min(
        prompt,
        max(normalized["cached_tokens"], normalized["cache_read_tokens"]),
    )
    normalized["cached_tokens"] = cached
    normalized["cache_read_tokens"] = min(prompt, normalized["cache_read_tokens"])
    normalized["non_cached_input_tokens"] = max(0, prompt - cached)
    normalized["input_tokens"] = prompt
    normalized["output_tokens"] = normalized["completion_tokens"]
    return normalized


def median_usage(samples: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    normalized = [normalize_usage(sample) for sample in samples]
    if not normalized:
        return normalize_usage({})
    return {
        key: int(median([sample[key] for sample in normalized]))
        for key in (*_USAGE_KEYS, "input_tokens", "non_cached_input_tokens", "output_tokens")
    }


def load_task_history(root: Path | None) -> list[dict[str, Any]]:
    if root is None or not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in root.glob("**/tasks/*/task.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("schema_version") != 2:
            continue
        records.append(payload)
    records.sort(
        key=lambda item: float(item.get("finished_at") or item.get("updated_at") or 0),
        reverse=True,
    )
    return records


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
    "load_task_history",
    "median_usage",
    "normalize_usage",
]
