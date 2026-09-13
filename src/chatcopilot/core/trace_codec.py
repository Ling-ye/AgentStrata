"""Pinned DeepEval DTO adapter. No instrumentation, evaluation or exporters."""
from __future__ import annotations

from functools import lru_cache
from importlib.metadata import version
import os
import math
from typing import Any

SDK_VERSION = "4.2.2"
SCHEMA = "agentstrata.local-trace/v1"


@lru_cache(maxsize=1)
def _types() -> tuple[Any, Any]:
    if version("deepeval") != SDK_VERSION:
        raise ValueError(f"Local traces require DeepEval {SDK_VERSION}")
    # Set vendor privacy flags before its package initializer can read dotenv or
    # configure telemetry. These are process-wide policy, never per-call secrets.
    os.environ.update({
        "DEEPEVAL_DISABLE_DOTENV": "1", "PYTHON_DOTENV_DISABLED": "1",
        "DEEPEVAL_TELEMETRY_OPT_OUT": "1", "DEEPEVAL_UPDATE_WARNING_OPT_IN": "0",
        "DEEPEVAL_FILE_SYSTEM": "READ_ONLY", "DEEPEVAL_NO_INSPECT_PROMPT": "1",
        "CONFIDENT_TRACE_VERBOSE": "0", "CONFIDENT_TRACE_FLUSH": "0",
    })
    from deepeval.tracing.api import BaseApiSpan, TraceApi
    return TraceApi, BaseApiSpan


def encode_trace(value: dict[str, Any]) -> dict[str, Any]:
    trace_type, span_type = _types()
    groups = ("baseSpans", "agentSpans", "llmSpans", "toolSpans", "retrieverSpans")
    spans = {key: [span_type.model_validate(span) for span in value.get(key, [])] for key in groups}
    return trace_type.model_validate({**value, **spans}).model_dump(mode="json", by_alias=True, exclude_none=True)


def validate_trace(value: dict[str, Any]) -> None:
    meta = (value.get("metadata") or {}).get("agentstrata") or {}
    if meta.get("schema") != SCHEMA or meta.get("sdk_version") != SDK_VERSION:
        raise ValueError("Unsupported local trace format")
    if (not isinstance(meta.get("source"), dict) or not isinstance(meta.get("artifacts"), dict)
            or meta.get("capture_state") not in {"available", "partial", "not_recorded"}
            or not isinstance(meta.get("capture_reasons"), list)
            or not isinstance(meta.get("execution_status"), str)):
        raise ValueError("Invalid local trace metadata")
    for key in ("started_at", "finished_at", "expires_at"):
        field = meta.get(key)
        if key == "expires_at" and field is None:
            continue
        if isinstance(field, bool) or not isinstance(field, (int, float)) or not math.isfinite(field) or field < 0:
            raise ValueError("Invalid local trace time")
    for digest, size in meta["artifacts"].items():
        if (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or type(size) is not int or size < 0):
            raise ValueError("Invalid trace artifact manifest")
    trace_type, _ = _types()
    trace_type.model_validate(value)
