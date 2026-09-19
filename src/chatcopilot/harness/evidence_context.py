"""Deterministic navigation over complete, read-only Harness evidence."""

from __future__ import annotations

from collections import Counter
from typing import Any

from chatcopilot.core.private_sqlite import json_text

_ADVERSE = {
    "failed",
    "error",
    "blocked",
    "cancelled",
    "interrupted",
    "not_run",
    "skipped",
    "timeout",
    "inconclusive",
}
_READ_ORDER = {
    "prepare": ("source", "reproduction"),
    "repair": ("source", "reproduction", "previous_attempts", "protected_cases"),
    "review": ("source", "patch", "verification", "regression", "reproduction"),
}
_INLINE_SECTION_BYTES = 4_096
_INLINE_TOTAL_BYTES = 12_288
_SPECIAL_INLINE_BYTES = {"source_index": 16_384, "failure_brief": 8_192, "target_context": 16_384}


def evidence_index(evidence: dict[str, Any], *, stage: str) -> dict[str, Any]:
    """Count actual status fields; never infer success from absent failures."""
    statuses: Counter[str] = Counter()
    adverse: list[dict[str, str]] = []
    adverse_count = 0

    def visit(value: Any, pointer: str) -> None:
        nonlocal adverse_count
        if isinstance(value, dict):
            for key, item in value.items():
                part = str(key).replace("~", "~0").replace("/", "~1")
                child = f"{pointer}/{part}"
                if key in {"status", "outcome"} and isinstance(item, str):
                    statuses[item] += 1
                    if item in _ADVERSE:
                        adverse_count += 1
                        if len(adverse) < 12:
                            adverse.append({"pointer": child, "value": item})
                elif (
                    key in {"error", "error_code", "missing", "failed_cases", "unverified"} and item
                ):
                    adverse_count += 1
                    if len(adverse) < 12:
                        adverse.append({"pointer": child, "value": "present"})
                visit(item, child)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{pointer}/{index}")

    visit(evidence, "")
    order = list(dict.fromkeys((*_READ_ORDER[stage], *evidence)))
    inline: dict[str, Any] = {}
    omitted: list[str] = []
    used = 0
    for key in order:
        if key not in evidence:
            continue
        size = len(json_text(evidence[key]).encode("utf-8"))
        limit = _SPECIAL_INLINE_BYTES.get(key, _INLINE_SECTION_BYTES)
        if size <= limit and (key in _SPECIAL_INLINE_BYTES or used + size <= _INLINE_TOTAL_BYTES):
            inline[key] = evidence[key]
            if key not in _SPECIAL_INLINE_BYTES:
                used += size
        else:
            omitted.append(key)
    return {
        "stage": stage,
        "read_order": [key for key in order if key in evidence],
        "sections": {
            key: {
                "pointer": "/" + key.replace("~", "~0").replace("/", "~1"),
                "bytes": len(json_text(value).encode("utf-8")),
                "items": len(value) if isinstance(value, (dict, list)) else None,
            }
            for key, value in evidence.items()
        },
        "inline_sections": inline,
        "omitted_sections": omitted,
        "inline_bytes": used + sum(len(json_text(value).encode("utf-8")) for key, value in inline.items()
                                     if key in _SPECIAL_INLINE_BYTES),
        "observed_status_counts": dict(statuses),
        "adverse_field_count": adverse_count,
        "adverse_locations": adverse,
        "adverse_locations_partial": adverse_count > len(adverse),
        "missing_stage_sections": [key for key in _READ_ORDER[stage] if key not in evidence],
    }
