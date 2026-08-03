"""Date normalization helpers for job and evidence recency filters."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

_CJK_DATE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")


def normalize_date(value: Any) -> str:
    """Return an ISO calendar date or an empty string for unknown input."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    match = _CJK_DATE.fullmatch(text)
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return ""
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return ""


def cutoff_date(days: int, *, today: date | None = None) -> str:
    if days < 0:
        raise ValueError("days 不能小于 0")
    return ((today or datetime.now(timezone.utc).date()) - timedelta(days=days)).isoformat()


def is_recent(value: str, days: int, *, today: date | None = None) -> bool:
    normalized = normalize_date(value)
    return bool(normalized and normalized >= cutoff_date(days, today=today))


__all__ = ["cutoff_date", "is_recent", "normalize_date"]
