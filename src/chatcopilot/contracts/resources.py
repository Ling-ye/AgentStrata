"""Channel resource bytes and the bounded fetch contract used by Application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chatcopilot.contracts.gateway import ResourceTicket


@dataclass(frozen=True)
class FetchedResource:
    """Bounded provider result; paths and URLs are deliberately absent."""

    data: bytes
    name: str | None = None
    media_type: str | None = None


class ResourceFetcherPort(Protocol):
    """Channel-owned fetch port that must stop reading after ``max_bytes``."""

    async def fetch(self, ticket: ResourceTicket, *, max_bytes: int) -> FetchedResource: ...
