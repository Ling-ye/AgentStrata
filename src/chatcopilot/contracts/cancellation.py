"""Thread-safe cooperative cancellation shared across runtime boundaries."""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable


class CancellationRequested(RuntimeError):
    """Signal that cooperative execution reached a cancellation checkpoint."""

    code = "cancelled"

    def __init__(self) -> None:
        super().__init__("cancellation requested")


@runtime_checkable
class CancellationProbe(Protocol):
    """Read-only cancellation surface accepted by Agent execution code."""

    def raise_if_cancelled(self) -> None:
        """Raise ``CancellationRequested`` after cancellation was requested."""


class CancellationToken:
    """One-way thread-safe cancellation source and probe."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancellationRequested()


class CombinedCancellation:
    def __init__(self, *probes: CancellationProbe | None):
        self.probes = tuple(probe for probe in probes if probe is not None)

    def raise_if_cancelled(self) -> None:
        for probe in self.probes:
            probe.raise_if_cancelled()


__all__ = [
    "CancellationProbe",
    "CancellationRequested",
    "CancellationToken",
]
