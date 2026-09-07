"""Validation for the service-owned, creation-time code revision snapshot."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


def validate_source_revision(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"status", "commit", "dirty", "captured_at"}:
        raise ValueError("Evaluation source revision fields are invalid")
    status, commit, dirty, captured_at = (value[key] for key in ("status", "commit", "dirty", "captured_at"))
    if not isinstance(captured_at, str):
        raise ValueError("Evaluation source revision capture time is invalid")
    try:
        timestamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Evaluation source revision capture time is invalid") from exc
    if timestamp.tzinfo is None:
        raise ValueError("Evaluation source revision capture time requires a timezone")
    valid_commit = isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)
    if not (
        (status == "recorded" and valid_commit and type(dirty) is bool)
        or (status == "partial" and valid_commit and dirty is None)
        or (status == "unavailable" and commit is None and dirty is None)
    ):
        raise ValueError("Evaluation source revision identity is invalid")
    return dict(value)
