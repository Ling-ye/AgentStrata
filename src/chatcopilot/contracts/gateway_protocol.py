"""Immutable data contracts for the AgentStrata Gateway v1 wire protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias, Union


GatewayScope: TypeAlias = Literal[
    "gateway.read",
    "chat.write",
    "chat.abort",
    "approvals.respond",
    "gateway.admin",
]


@dataclass(frozen=True)
class RequestFrame:
    request_id: str
    method: str
    params: Mapping[str, Any] = field(default_factory=dict, repr=False)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ResponseError:
    code: str
    message: str
    data: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResponseFrame:
    request_id: str
    ok: bool
    result: Mapping[str, Any] | None = None
    error: ResponseError | None = None


@dataclass(frozen=True)
class EventFrame:
    event: str
    seq: int
    payload: Mapping[str, Any] = field(default_factory=dict)


GatewayFrame = Union[RequestFrame, ResponseFrame, EventFrame]


@dataclass(frozen=True)
class ConnectChallenge:
    nonce: str
    issued_at_ms: int
    expires_at_ms: int
    min_protocol: int
    max_protocol: int


@dataclass(frozen=True)
class ConnectRequest:
    nonce: str
    min_protocol: int
    max_protocol: int
    client_id: str
    client_version: str
    client_mode: str
    scopes: tuple[GatewayScope, ...]
    capabilities: tuple[str, ...]
    auth_token: str = field(repr=False)


@dataclass(frozen=True)
class HelloOk:
    protocol: int
    client_id: str
    scopes: tuple[GatewayScope, ...]
    methods: tuple[str, ...]
    events: tuple[str, ...]
    server_generation: int
    event_cursor: int
    policy_version: str
    limits: Mapping[str, int]


__all__ = [
    "ConnectChallenge",
    "ConnectRequest",
    "EventFrame",
    "GatewayFrame",
    "GatewayScope",
    "HelloOk",
    "RequestFrame",
    "ResponseError",
    "ResponseFrame",
]
