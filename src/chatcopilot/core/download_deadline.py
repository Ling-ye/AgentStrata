"""Socket wait budgets for a download's shared monotonic deadline."""
from __future__ import annotations

import http.client
import socket
import time

from chatcopilot.contracts.resources import DOWNLOAD_IO_TIMEOUT_SECONDS


def remaining_download_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Download batch time budget exhausted")
    return min(DOWNLOAD_IO_TIMEOUT_SECONDS, remaining)


def apply_download_timeout(connection: http.client.HTTPConnection, deadline: float,
                           *, sock: socket.socket | None = None) -> None:
    connection.timeout = remaining_download_seconds(deadline)
    current = sock if sock is not None else connection.sock
    # A Connection: close response may release the final descriptor as soon as
    # its declared body length is consumed, before the caller's next EOF read.
    if current is not None and current.fileno() != -1:
        current.settimeout(connection.timeout)
