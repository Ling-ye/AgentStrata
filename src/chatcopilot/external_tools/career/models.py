"""Domain models shared by career providers and persistence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

PROVIDER_DIRECT = "direct"
PROVIDER_RESEARCH_FALLBACK = "research_fallback"
PROVIDER_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class JobListing:
    company: str
    title: str
    location: str
    source_url: str
    source_job_id: str = ""
    responsibilities: str = ""
    requirements: str = ""
    published_at: str = ""
    published_on: str = ""
    source: str = "official"
    source_mode: str = PROVIDER_DIRECT

    @property
    def identity(self) -> str:
        if self.source_job_id:
            return f"{self.company}:{self.source_job_id}"
        raw = "\n".join((self.company, self.title, self.location, self.source_url))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        payload = asdict(self)
        payload.pop("published_at", None)
        payload.pop("published_on", None)
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    company: str
    ok: bool
    jobs: tuple[JobListing, ...] = ()
    error: str = ""
    fallback_query: str = ""
    source_url: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    mode: str = PROVIDER_DIRECT
    snapshot_complete: bool = False


__all__ = [
    "JobListing",
    "PROVIDER_DIRECT",
    "PROVIDER_RESEARCH_FALLBACK",
    "PROVIDER_UNAVAILABLE",
    "ProviderResult",
]
