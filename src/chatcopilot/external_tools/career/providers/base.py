"""Provider contracts and small public HTTP client."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from threading import Lock
from typing import Any, ClassVar

from chatcopilot.external_tools.career.models import ProviderResult


class PublicHttpClient:
    """Read-only HTTP client with an in-process TTL cache."""

    _cache: ClassVar[dict[str, tuple[float, bytes]]] = {}
    _last_request: ClassVar[dict[str, float]] = {}
    _lock: ClassVar[Lock] = Lock()

    def __init__(
        self,
        *,
        timeout_seconds: int = 15,
        ttl_seconds: int = 900,
        min_request_interval_seconds: float = 0.2,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.ttl_seconds = ttl_seconds
        self.min_request_interval_seconds = min_request_interval_seconds

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        target = f"{url}?{urllib.parse.urlencode(params)}"
        raw = self._get(target)
        return json.loads(raw.decode("utf-8"))

    def _get(self, url: str) -> bytes:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(url)
            if cached and cached[0] > now:
                return cached[1]
            host = urllib.parse.urlparse(url).netloc
            wait_seconds = max(
                0.0,
                self.min_request_interval_seconds - (now - self._last_request.get(host, 0.0)),
            )
            self._last_request[host] = now + wait_seconds
        if wait_seconds:
            time.sleep(wait_seconds)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "AgentStrata-CareerIntel/1.0 (+public-career-pages)"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read()
        with self._lock:
            self._cache[url] = (now + self.ttl_seconds, raw)
        return raw


class CareerSourceProvider(ABC):
    id: str
    company: str
    aliases: tuple[str, ...]
    source_url: str
    official_job_hosts: tuple[str, ...] = ()

    @abstractmethod
    def search(
        self,
        *,
        keywords: tuple[str, ...],
        locations: tuple[str, ...],
        limit: int,
        posted_within_days: int,
    ) -> ProviderResult:
        """Return normalized public jobs or an actionable unavailable result."""

    def matches_company(self, value: str) -> bool:
        normalized = value.strip().casefold()
        return normalized == self.company.casefold() or normalized in {
            alias.casefold() for alias in self.aliases
        }

    def validate_job_url(self, source_url: str) -> None:
        """Reject a persisted job URL outside this provider's official hosts."""
        parsed = urllib.parse.urlparse(source_url)
        host = (parsed.hostname or "").casefold()
        if self.official_job_hosts and not any(
            host == suffix or host.endswith("." + suffix)
            for suffix in self.official_job_hosts
        ):
            raise ValueError(
                f"{self.company} fallback 岗位必须使用该公司官方招聘域名"
            )


def fallback_query(company: str, source_url: str, keywords: tuple[str, ...]) -> str:
    domain = urllib.parse.urlparse(source_url).netloc
    terms = " OR ".join(dict.fromkeys(keywords))
    return f'site:{domain} {company} ({terms}) 招聘'
