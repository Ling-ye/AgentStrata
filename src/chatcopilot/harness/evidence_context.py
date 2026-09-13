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
        "inline_sections": {
            key: value
            for key, value in evidence.items()
            if len(json_text(value).encode("utf-8")) <= 4_096
        },
        "observed_status_counts": dict(statuses),
        "adverse_field_count": adverse_count,
        "adverse_locations": adverse,
        "adverse_locations_partial": adverse_count > len(adverse),
        "missing_stage_sections": [key for key in _READ_ORDER[stage] if key not in evidence],
    }
