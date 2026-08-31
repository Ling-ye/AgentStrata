"""Strict AgentStrata Gateway v1 frame, handshake, and access validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from chatcopilot.contracts.gateway_protocol import (
    ConnectChallenge,
    ConnectRequest,
    EventFrame,
    GatewayFrame,
    GatewayScope,
    HelloOk,
    RequestFrame,
    ResponseError,
    ResponseFrame,
)


PROTOCOL_MIN_VERSION = 1
PROTOCOL_MAX_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_COLLECTION_ITEMS = 1024
MAX_STRING_CHARS = 256 * 1024
MAX_PENDING_CHALLENGES = 1024

GATEWAY_SCOPES: tuple[GatewayScope, ...] = (
    "gateway.read",
    "chat.write",
    "chat.abort",
    "approvals.respond",
    "gateway.admin",
)
GATEWAY_METHODS = (
    "health",
    "status",
    "channels.list",
    "events.replay",
    "sessions.create",
    "sessions.list",
    "sessions.get",
    "sessions.patch",
    "chat.send",
    "chat.abort",
    "runs.get",
    "runs.latest",
    "deliveries.get",
    "approvals.list",
    "approvals.resolve",
)
GATEWAY_EVENTS = (
    "channel.status",
    "session.updated",
    "chat.update",
    "chat.final",
    "chat.error",
    "approval.requested",
    "delivery.updated",
)
CONNECT_METHOD = "connect"
CONNECT_CHALLENGE_EVENT = "connect.challenge"

METHOD_SCOPE: Mapping[str, GatewayScope] = {
    "health": "gateway.read",
    "status": "gateway.read",
    "channels.list": "gateway.read",
    "events.replay": "gateway.read",
    "sessions.create": "chat.write",
    "sessions.list": "gateway.read",
    "sessions.get": "gateway.read",
    "sessions.patch": "chat.write",
    "chat.send": "chat.write",
    "chat.abort": "chat.abort",
    "runs.get": "gateway.read",
    "runs.latest": "gateway.read",
    "deliveries.get": "gateway.read",
    "approvals.list": "approvals.respond",
    "approvals.resolve": "approvals.respond",
}
EVENT_SCOPE: Mapping[str, GatewayScope] = {
    "channel.status": "gateway.read",
    "session.updated": "gateway.read",
    "chat.update": "gateway.read",
    "chat.final": "gateway.read",
    "chat.error": "gateway.read",
    "approval.requested": "approvals.respond",
    "delivery.updated": "gateway.read",
}
MUTATION_METHODS = frozenset(
    {
        "sessions.create",
        "sessions.patch",
        "chat.send",
        "chat.abort",
        "approvals.resolve",
    }
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CAPABILITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class GatewayProtocolError(ValueError):
    """Fail-closed protocol error with a stable public error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class GatewayCredentialBinding:
    """One strong token bound to exactly one transport client authority."""

    token: str = field(repr=False)
    client_id: str
    client_mode: str
    scopes: tuple[GatewayScope, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or _TOKEN_RE.fullmatch(self.token) is None:
            raise ValueError("Gateway credential token must be 32-128 URL-safe characters")
        if not isinstance(self.client_id, str) or _IDENTIFIER_RE.fullmatch(self.client_id) is None:
            raise ValueError("Gateway credential client_id is invalid")
        if (
            not isinstance(self.client_mode, str)
            or _IDENTIFIER_RE.fullmatch(self.client_mode) is None
        ):
            raise ValueError("Gateway credential client_mode is invalid")
        scopes = tuple(self.scopes)
        object.__setattr__(self, "scopes", scopes)
        if not scopes or len(set(scopes)) != len(scopes) or set(scopes).difference(GATEWAY_SCOPES):
            raise ValueError("Gateway credential scopes must be unique known scopes")


class GatewayCredentialAuthority(Protocol):
    """Authenticate one connect request without exposing credential metadata."""

    def authenticate(self, request: ConnectRequest) -> None: ...


class StaticGatewayCredentialAuthority:
    """Fail-closed in-memory authority for explicitly configured client credentials."""

    def __init__(self, credentials: Collection[GatewayCredentialBinding]) -> None:
        configured = tuple(credentials)
        if not configured:
            raise ValueError("at least one Gateway credential is required")
        digests: set[bytes] = set()
        bindings: list[tuple[bytes, str, str, frozenset[GatewayScope]]] = []
        for credential in configured:
            if not isinstance(credential, GatewayCredentialBinding):
                raise TypeError("credentials must contain GatewayCredentialBinding values")
            digest = hashlib.sha256(credential.token.encode("utf-8")).digest()
            if digest in digests:
                raise ValueError("Gateway credential tokens must be unique")
            digests.add(digest)
            bindings.append(
                (
                    digest,
                    credential.client_id,
                    credential.client_mode,
                    frozenset(credential.scopes),
                )
            )
        self._bindings = tuple(bindings)

    def authenticate(self, request: ConnectRequest) -> None:
        supplied = hashlib.sha256(request.auth_token.encode("utf-8")).digest()
        matched: tuple[bytes, str, str, frozenset[GatewayScope]] | None = None
        for binding in self._bindings:
            if secrets.compare_digest(supplied, binding[0]):
                matched = binding
        if (
            matched is None
            or request.client_id != matched[1]
            or request.client_mode != matched[2]
            or frozenset(request.scopes) != matched[3]
        ):
            raise GatewayProtocolError(
                "authentication_failed",
                "Gateway authentication failed",
            )

    def __repr__(self) -> str:
        return "StaticGatewayCredentialAuthority(credentials=<redacted>)"


class GatewayHandshakeAuthority:
    """Issue one-use challenges and authenticate the first ``connect`` request."""

    def __init__(
        self,
        *,
        credential_authority: GatewayCredentialAuthority,
        server_generation: int,
        event_cursor: int = 0,
        policy_version: str = "1",
        challenge_ttl_seconds: float = 10.0,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if type(server_generation) is not int or server_generation < 1:
            raise ValueError("server_generation must be positive")
        if type(event_cursor) is not int or event_cursor < 0:
            raise ValueError("event_cursor cannot be negative")
        if not math.isfinite(challenge_ttl_seconds) or challenge_ttl_seconds <= 0:
            raise ValueError("challenge_ttl_seconds must be positive")
        self._credential_authority = credential_authority
        self._server_generation = server_generation
        self._event_cursor = event_cursor
        self._policy_version = _required_string(policy_version, "policy_version", 128)
        self._challenge_ttl_seconds = challenge_ttl_seconds
        self._clock = clock
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(32))
        self._pending: dict[str, ConnectChallenge] = {}

    def issue_challenge(self) -> ConnectChallenge:
        now_ms = _clock_milliseconds(self._clock)
        self._prune_expired(now_ms)
        if len(self._pending) >= MAX_PENDING_CHALLENGES:
            raise GatewayProtocolError(
                "challenge_capacity",
                "too many Gateway handshakes are pending",
            )
        nonce = self._nonce_factory()
        if not _TOKEN_RE.fullmatch(nonce) or nonce in self._pending:
            raise GatewayProtocolError(
                "challenge_generation_failed",
                "Gateway challenge generation failed",
            )
        challenge = ConnectChallenge(
            nonce=nonce,
            issued_at_ms=now_ms,
            expires_at_ms=now_ms + int(self._challenge_ttl_seconds * 1000),
            min_protocol=PROTOCOL_MIN_VERSION,
            max_protocol=PROTOCOL_MAX_VERSION,
        )
        self._pending[nonce] = challenge
        return challenge

    def accept(self, frame: RequestFrame) -> HelloOk:
        if frame.method != CONNECT_METHOD:
            raise GatewayProtocolError(
                "connect_required",
                "the first client request must be connect",
            )
        raw_nonce = frame.params.get("nonce")
        if not isinstance(raw_nonce, str):
            raise GatewayProtocolError("invalid_connect", "connect nonce is required")
        challenge = self._pending.pop(raw_nonce, None)
        if challenge is None:
            raise GatewayProtocolError(
                "invalid_challenge",
                "Gateway challenge is unknown or already consumed",
            )

        now_ms = _clock_milliseconds(self._clock)
        if now_ms >= challenge.expires_at_ms:
            raise GatewayProtocolError("challenge_expired", "Gateway challenge has expired")
        request = parse_connect_request(frame)
        try:
            self._credential_authority.authenticate(request)
        except Exception:
            raise GatewayProtocolError(
                "authentication_failed",
                "Gateway authentication failed",
            ) from None

        lower = max(PROTOCOL_MIN_VERSION, request.min_protocol)
        upper = min(PROTOCOL_MAX_VERSION, request.max_protocol)
        if lower > upper:
            raise GatewayProtocolError(
                "protocol_version_mismatch",
                "client and Gateway protocol ranges do not overlap",
            )
        return HelloOk(
            protocol=upper,
            client_id=request.client_id,
            scopes=request.scopes,
            methods=methods_for_scopes(request.scopes),
            events=events_for_scopes(request.scopes),
            server_generation=self._server_generation,
            event_cursor=self._event_cursor,
            policy_version=self._policy_version,
            limits={
                "maxFrameBytes": MAX_FRAME_BYTES,
                "maxCollectionItems": MAX_COLLECTION_ITEMS,
                "maxStringChars": MAX_STRING_CHARS,
            },
        )

    def challenge_event(self, challenge: ConnectChallenge) -> EventFrame:
        return EventFrame(
            event=CONNECT_CHALLENGE_EVENT,
            seq=0,
            payload={
                "nonce": challenge.nonce,
                "issuedAt": challenge.issued_at_ms,
                "expiresAt": challenge.expires_at_ms,
                "minProtocol": challenge.min_protocol,
                "maxProtocol": challenge.max_protocol,
            },
        )

    @staticmethod
    def hello_response(request_id: str, hello: HelloOk) -> ResponseFrame:
        return ResponseFrame(
            request_id=request_id,
            ok=True,
            result={
                "type": "hello-ok",
                "protocol": hello.protocol,
                "clientId": hello.client_id,
                "scopes": list(hello.scopes),
                "methods": list(hello.methods),
                "events": list(hello.events),
                "serverGeneration": hello.server_generation,
                "eventCursor": hello.event_cursor,
                "policyVersion": hello.policy_version,
                "limits": dict(hello.limits),
            },
        )

    def _prune_expired(self, now_ms: int) -> None:
        expired = [
            nonce for nonce, challenge in self._pending.items() if now_ms >= challenge.expires_at_ms
        ]
        for nonce in expired:
            self._pending.pop(nonce, None)


def parse_connect_request(frame: RequestFrame) -> ConnectRequest:
    if frame.method != CONNECT_METHOD or frame.idempotency_key is not None:
        raise GatewayProtocolError("invalid_connect", "connect request shape is invalid")
    params = _object(frame.params, "connect params")
    _exact_keys(
        params,
        required={
            "nonce",
            "minProtocol",
            "maxProtocol",
            "client",
            "scopes",
            "capabilities",
            "auth",
        },
        label="connect params",
    )
    client = _object(params["client"], "connect client")
    _exact_keys(client, required={"id", "version", "mode"}, label="connect client")
    auth = _object(params["auth"], "connect auth")
    _exact_keys(auth, required={"token"}, label="connect auth")
    scopes = _string_tuple(params["scopes"], "connect scopes", allowed=GATEWAY_SCOPES)
    capabilities = _string_tuple(
        params["capabilities"],
        "connect capabilities",
        pattern=_CAPABILITY_RE,
    )
    min_protocol = _plain_int(params["minProtocol"], "minProtocol")
    max_protocol = _plain_int(params["maxProtocol"], "maxProtocol")
    if min_protocol < 1 or max_protocol < min_protocol:
        raise GatewayProtocolError(
            "invalid_connect",
            "connect protocol range is invalid",
        )
    token = _required_string(auth["token"], "connect token", 128)
    if not _TOKEN_RE.fullmatch(token):
        raise GatewayProtocolError("invalid_connect", "connect token shape is invalid")
    return ConnectRequest(
        nonce=_required_identifier(params["nonce"], "connect nonce", pattern=_TOKEN_RE),
        min_protocol=min_protocol,
        max_protocol=max_protocol,
        client_id=_required_identifier(client["id"], "client id"),
        client_version=_required_string(client["version"], "client version", 64),
        client_mode=_required_identifier(client["mode"], "client mode"),
        scopes=cast(tuple[GatewayScope, ...], scopes),
        capabilities=capabilities,
        auth_token=token,
    )


def methods_for_scopes(scopes: Collection[GatewayScope]) -> tuple[str, ...]:
    granted = frozenset(scopes)
    return tuple(method for method in GATEWAY_METHODS if METHOD_SCOPE[method] in granted)


def events_for_scopes(scopes: Collection[GatewayScope]) -> tuple[str, ...]:
    granted = frozenset(scopes)
    return tuple(event for event in GATEWAY_EVENTS if EVENT_SCOPE[event] in granted)


def validate_request_access(
    frame: RequestFrame,
    *,
    scopes: Collection[GatewayScope],
) -> None:
    if frame.method == CONNECT_METHOD:
        raise GatewayProtocolError(
            "already_connected",
            "connect is only valid as the first request",
        )
    required = METHOD_SCOPE.get(frame.method)
    if required is None:
        raise GatewayProtocolError("unknown_method", "Gateway method is not recognized")
    if required not in scopes:
        raise GatewayProtocolError("scope_denied", "Gateway scope does not allow this method")
    if frame.method in MUTATION_METHODS:
        if frame.idempotency_key is None:
            raise GatewayProtocolError(
                "idempotency_key_required",
                "Gateway mutation requires an idempotency key",
            )
        _validate_idempotency_key(frame.idempotency_key)
    elif frame.idempotency_key is not None:
        _validate_idempotency_key(frame.idempotency_key)


def request_fingerprint(frame: RequestFrame) -> str:
    canonical = _canonical_json({"method": frame.method, "params": dict(frame.params)})
    return hashlib.sha256(canonical).hexdigest()


def encode_frame(frame: GatewayFrame, *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    payload = frame_to_dict(frame)
    encoded = _canonical_json(payload)
    if not encoded or len(encoded) > max_bytes:
        raise GatewayProtocolError("frame_too_large", "Gateway frame exceeds the byte limit")
    return encoded


def decode_frame(raw: bytes | str, *, max_bytes: int = MAX_FRAME_BYTES) -> GatewayFrame:
    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise GatewayProtocolError(
                "invalid_json", "Gateway frame is not valid UTF-8 JSON"
            ) from exc
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise GatewayProtocolError("invalid_frame", "Gateway frame must be bytes or text")
    if not encoded or len(encoded) > max_bytes:
        raise GatewayProtocolError("frame_too_large", "Gateway frame exceeds the byte limit")
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise GatewayProtocolError("invalid_json", "Gateway frame is not valid UTF-8 JSON") from exc
    _validate_json_shape(value)
    payload = _object(value, "Gateway frame")
    frame_type = payload.get("type")
    if frame_type == "req":
        return _parse_request(payload)
    if frame_type == "res":
        return _parse_response(payload)
    if frame_type == "event":
        return _parse_event(payload)
    raise GatewayProtocolError("invalid_frame", "Gateway frame type is not recognized")


def frame_to_dict(frame: GatewayFrame) -> dict[str, Any]:
    if isinstance(frame, RequestFrame):
        payload: dict[str, Any] = {
            "type": "req",
            "id": frame.request_id,
            "method": frame.method,
            "params": dict(frame.params),
        }
        if frame.idempotency_key is not None:
            payload["idempotencyKey"] = frame.idempotency_key
        _parse_request(payload)
    elif isinstance(frame, ResponseFrame):
        payload = {"type": "res", "id": frame.request_id, "ok": frame.ok}
        if frame.ok:
            payload["result"] = dict(frame.result or {})
        elif frame.error is not None:
            error: dict[str, Any] = {
                "code": frame.error.code,
                "message": frame.error.message,
            }
            if frame.error.data is not None:
                error["data"] = dict(frame.error.data)
            payload["error"] = error
        _parse_response(payload)
    elif isinstance(frame, EventFrame):
        payload = {
            "type": "event",
            "event": frame.event,
            "seq": frame.seq,
            "payload": dict(frame.payload),
        }
        _parse_event(payload)
    else:
        raise GatewayProtocolError("invalid_frame", "unsupported Gateway frame object")
    _validate_json_shape(payload)
    return payload


def _parse_request(payload: Mapping[str, Any]) -> RequestFrame:
    _exact_keys(
        payload,
        required={"type", "id", "method", "params"},
        optional={"idempotencyKey"},
        label="request frame",
    )
    method = _required_identifier(payload["method"], "request method")
    if method != CONNECT_METHOD and method not in GATEWAY_METHODS:
        raise GatewayProtocolError("unknown_method", "Gateway method is not recognized")
    idempotency_key = payload.get("idempotencyKey")
    if idempotency_key is not None:
        idempotency_key = _validate_idempotency_key(idempotency_key)
    return RequestFrame(
        request_id=_required_identifier(payload["id"], "request id"),
        method=method,
        params=_object(payload["params"], "request params"),
        idempotency_key=idempotency_key,
    )


def _parse_response(payload: Mapping[str, Any]) -> ResponseFrame:
    _exact_keys(
        payload,
        required={"type", "id", "ok"},
        optional={"result", "error"},
        label="response frame",
    )
    ok = payload["ok"]
    if type(ok) is not bool:
        raise GatewayProtocolError("invalid_frame", "response ok must be a boolean")
    result_present = "result" in payload
    error_present = "error" in payload
    if ok and error_present:
        raise GatewayProtocolError("invalid_frame", "successful response cannot contain error")
    if not ok and (result_present or not error_present):
        raise GatewayProtocolError("invalid_frame", "failed response must contain only error")
    if ok:
        result = _object(payload.get("result", {}), "response result")
        return ResponseFrame(
            request_id=_required_identifier(payload["id"], "response id"),
            ok=True,
            result=result,
        )
    error = _object(payload["error"], "response error")
    _exact_keys(
        error,
        required={"code", "message"},
        optional={"data"},
        label="response error",
    )
    data = error.get("data")
    return ResponseFrame(
        request_id=_required_identifier(payload["id"], "response id"),
        ok=False,
        error=ResponseError(
            code=_required_identifier(error["code"], "response error code"),
            message=_required_string(error["message"], "response error message", 4096),
            data=_object(data, "response error data") if data is not None else None,
        ),
    )


def _parse_event(payload: Mapping[str, Any]) -> EventFrame:
    _exact_keys(
        payload,
        required={"type", "event", "seq", "payload"},
        label="event frame",
    )
    event = _required_identifier(payload["event"], "event name")
    seq = _plain_int(payload["seq"], "event seq")
    if event == CONNECT_CHALLENGE_EVENT:
        if seq != 0:
            raise GatewayProtocolError("invalid_frame", "connect challenge seq must be zero")
    elif event not in GATEWAY_EVENTS or seq < 1:
        raise GatewayProtocolError("invalid_frame", "Gateway event or sequence is invalid")
    return EventFrame(event=event, seq=seq, payload=_object(payload["payload"], "event payload"))


def _canonical_json(value: Any) -> bytes:
    _validate_json_shape(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GatewayProtocolError("invalid_json", "Gateway value is not valid JSON") from exc


def _validate_json_shape(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise GatewayProtocolError("invalid_json", "Gateway JSON nesting is too deep")
        if isinstance(current, Mapping):
            if len(current) > MAX_COLLECTION_ITEMS:
                raise GatewayProtocolError("invalid_json", "Gateway JSON object is too large")
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise GatewayProtocolError("invalid_json", "Gateway JSON key is invalid")
                stack.append((item, depth + 1))
        elif isinstance(current, (list, tuple)):
            if len(current) > MAX_COLLECTION_ITEMS:
                raise GatewayProtocolError("invalid_json", "Gateway JSON array is too large")
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            if len(current) > MAX_STRING_CHARS:
                raise GatewayProtocolError("invalid_json", "Gateway JSON string is too large")
        elif type(current) is float:
            if not math.isfinite(current):
                raise GatewayProtocolError("invalid_json", "Gateway JSON number is invalid")
        elif current is None or type(current) in {bool, int}:
            continue
        else:
            raise GatewayProtocolError("invalid_json", "Gateway value is not valid JSON")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise GatewayProtocolError("invalid_frame", f"{label} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    label: str,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise GatewayProtocolError("invalid_frame", f"{label} fields are invalid")


def _required_string(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise GatewayProtocolError("invalid_frame", f"{label} is invalid")
    return value


def _required_identifier(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str] = _IDENTIFIER_RE,
) -> str:
    text = _required_string(value, label, 256)
    if not pattern.fullmatch(text):
        raise GatewayProtocolError("invalid_frame", f"{label} is invalid")
    return text


def _plain_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise GatewayProtocolError("invalid_frame", f"{label} must be an integer")
    return value


def _string_tuple(
    value: Any,
    label: str,
    *,
    allowed: Collection[str] | None = None,
    pattern: re.Pattern[str] = _IDENTIFIER_RE,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise GatewayProtocolError("invalid_frame", f"{label} must be an array")
    result = tuple(_required_identifier(item, label, pattern=pattern) for item in value)
    if len(set(result)) != len(result):
        raise GatewayProtocolError("invalid_frame", f"{label} cannot contain duplicates")
    if allowed is not None and not set(result).issubset(allowed):
        raise GatewayProtocolError("invalid_frame", f"{label} contains an unknown value")
    return result


def _validate_idempotency_key(value: Any) -> str:
    return _required_identifier(value, "idempotency key", pattern=_IDEMPOTENCY_KEY_RE)


def _clock_milliseconds(clock: Callable[[], float]) -> int:
    observed_at = clock()
    if type(observed_at) not in {int, float} or not math.isfinite(observed_at):
        raise GatewayProtocolError("invalid_clock", "Gateway clock returned an invalid value")
    if observed_at < 0:
        raise GatewayProtocolError("invalid_clock", "Gateway clock returned an invalid value")
    return int(observed_at * 1000)


__all__ = [
    "CONNECT_CHALLENGE_EVENT",
    "CONNECT_METHOD",
    "GATEWAY_EVENTS",
    "GATEWAY_METHODS",
    "GATEWAY_SCOPES",
    "EVENT_SCOPE",
    "MAX_FRAME_BYTES",
    "METHOD_SCOPE",
    "MUTATION_METHODS",
    "PROTOCOL_MAX_VERSION",
    "PROTOCOL_MIN_VERSION",
    "GatewayCredentialAuthority",
    "GatewayCredentialBinding",
    "GatewayHandshakeAuthority",
    "GatewayProtocolError",
    "StaticGatewayCredentialAuthority",
    "decode_frame",
    "encode_frame",
    "events_for_scopes",
    "frame_to_dict",
    "methods_for_scopes",
    "parse_connect_request",
    "request_fingerprint",
    "validate_request_access",
]
