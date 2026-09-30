"""Channel resource bytes and the bounded fetch contract used by Application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chatcopilot.contracts.gateway import ResourceTicket


INBOUND_FILE_MAX_BYTES = 64 * 1024 * 1024
OUTBOUND_RESOURCE_SOURCE_CHARS = 64 * 1024 * 1024
OUTBOUND_FILE_MAX_BYTES = ((OUTBOUND_RESOURCE_SOURCE_CHARS - len("base64://")) // 4) * 3
DOWNLOAD_IO_TIMEOUT_SECONDS = 60.0
DOWNLOAD_BATCH_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class FetchedResource:
    """Bounded provider result; paths and URLs are deliberately absent."""

    data: bytes
    name: str | None = None
    media_type: str | None = None


class ResourceFetcherPort(Protocol):
    """Channel-owned fetch port that must stop reading after ``max_bytes``."""

    async def fetch(self, ticket: ResourceTicket, *, max_bytes: int) -> FetchedResource: ...
