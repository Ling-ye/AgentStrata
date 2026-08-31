"""Runtime SPI implemented by trusted Channel drivers."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Awaitable, Callable, Literal, Protocol

from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    DeliveryReceipt,
    OutboundEnvelope,
)


ChannelState = Literal["stopped", "connecting", "ready", "error"]
InboundEventHandler = Callable[[CanonicalInboundEvent], Awaitable[None]]
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class ChannelDeliveryError(RuntimeError):
    """Provider-neutral delivery failure with a stable, secret-free code."""

    def __init__(self, code: str, message: str) -> None:
        if not _ERROR_CODE_RE.fullmatch(code):
            raise ValueError("Channel delivery error code is invalid")
        self.code = code
        RuntimeError.__init__(self, message)


class ChannelDefinitelyNotSubmittedError(ChannelDeliveryError):
    """The observed failure proves the provider did not accept the action."""


class ChannelDeliveryUnknownError(ChannelDeliveryError):
    """The action may have reached the provider but no acknowledgement was observed."""


@dataclass(frozen=True)
class ChannelHealth:
    """Secret-free snapshot of one Channel driver's current connection state."""

    channel_id: str
    account: ChannelAccountRef
    state: ChannelState
    connection_generation: str | None = None
    detail_code: str | None = None


class ChannelDriver(Protocol):
    """Minimal lifecycle and delivery surface owned by the Gateway."""

    @property
    def channel_id(self) -> str: ...

    async def start(self) -> None:
        """Return after scheduling bounded readers; never await inbound inline."""
        ...

    async def stop(self) -> None: ...

    def health(self) -> ChannelHealth: ...

    async def send(self, envelope: OutboundEnvelope) -> DeliveryReceipt: ...


__all__ = [
    "ChannelDefinitelyNotSubmittedError",
    "ChannelDeliveryError",
    "ChannelDeliveryUnknownError",
    "ChannelDriver",
    "ChannelHealth",
    "ChannelState",
    "InboundEventHandler",
]
