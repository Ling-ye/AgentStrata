"""Stable scan scope identifiers for comparable job snapshots."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def build_scope(
    *,
    company: str,
    provider: str,
    source_mode: str,
    keywords: Iterable[str],
    locations: Iterable[str],
    posted_within_days: int,
) -> dict[str, Any]:
    payload = {
        "company": company.strip(),
        "provider": provider.strip(),
        "source_mode": source_mode.strip(),
        "keywords": sorted({value.strip().casefold() for value in keywords if value.strip()}),
        "locations": sorted({value.strip().casefold() for value in locations if value.strip()}),
        "posted_within_days": int(posted_within_days),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["scope_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return payload


__all__ = ["build_scope"]

