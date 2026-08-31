"""Private SQLite persistence for Gateway fencing, replay, ingress, and outbox."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from chatcopilot.contracts.authorization import (
    ApprovalRequest,
    ApprovalResolution,
    AuthorizationDecision,
    Principal,
)
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    DeliveryReceipt,
    DeliveryStage,
    MessageSegment,
    OutboundEnvelope,
    ResourceTicket,
    SenderClaim,
    TransportEvidence,
)
from chatcopilot.contracts.identity import ConversationIdentity, Role

from .protocol import GATEWAY_EVENTS


SCHEMA_VERSION = 2
MAX_STATE_JSON_BYTES = 1024 * 1024
_INSTANCE_LEASE_FILENAME = "gateway.instance.lock"
INGRESS_STATES = frozenset({"accepted", "processing", "completed", "failed", "recovery_required"})
OUTBOX_STATES = frozenset(
    {
        "pending",
        "submitting",
        "provider_submitted",
        "provider_acknowledged",
        "delivery_unknown",
        "failed",
    }
)
DELIVERY_STAGES = frozenset(
    {
        "gateway_accepted",
        "provider_submitted",
        "provider_acknowledged",
        "delivery_unknown",
        "platform_displayed",
        "user_read",
        "failed",
    }
)
RUN_STATES = frozenset(
    {
        "accepted",
        "running",
        "abort_requested",
        "recovery_required",
        "completed",
        "aborted",
        "failed",
    }
)
ACTIVE_RUN_STATES = frozenset({"accepted", "running", "abort_requested", "recovery_required"})
TERMINAL_RUN_STATES = frozenset({"completed", "aborted", "failed"})
APPROVAL_STATUSES = frozenset({"pending", "resolved", "expired"})
RunState: TypeAlias = Literal[
    "accepted",
    "running",
    "abort_requested",
    "recovery_required",
    "completed",
    "aborted",
    "failed",
]
RunOutcome: TypeAlias = Literal["completed", "aborted", "failed"]

_SESSION_MODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_APPROVAL_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_SESSION_COLUMNS = (
    "channel, account_id, conversation_kind, conversation_id, writer_generation, "
    "mode, debug, event_cursor, active_run_id, created_at, updated_at"
)
_RUN_COLUMNS = (
    "session_id, input_fingerprint, state, generation, result_json, error_code, "
    "recovery_from_state, created_at, started_at, abort_requested_at, finished_at, updated_at"
)
_APPROVAL_COLUMNS = (
    "session_id, run_id, operation, target, params_digest, actor_ref, conversation_ref, "
    "policy_version, challenge_digest, challenge, expires_at, state, decision_id, "
    "accepted, decided_at, generation, created_at, updated_at"
)


class GatewayStateError(RuntimeError):
    """Base class for durable Gateway state failures."""


class GatewayInstanceLeaseUnavailable(GatewayStateError):
    """Raised when another process already owns the instance lease."""


class StaleWriterGeneration(GatewayStateError):
    """Raised when a superseded Gateway process attempts a mutation."""


class IdempotencyConflict(GatewayStateError):
    """Raised when one idempotency identity is reused with different parameters."""


class IngressConflict(GatewayStateError):
    """Raised when a provider event identity is reused with different evidence."""


class OutboundConflict(GatewayStateError):
    """Raised when an outbound identity is reused with a different envelope."""


class SessionConflict(GatewayStateError):
    """Raised when a session identity or immutable conversation binding conflicts."""


class RunConflict(GatewayStateError):
    """Raised when a run identity or lifecycle transition conflicts."""


class ApprovalConflict(GatewayStateError):
    """Raised when an approval identity or immutable binding conflicts."""


@dataclass(frozen=True)
class IdempotencyReservation:
    state: Literal["reserved", "pending", "completed", "recovery_required"]
    generation: int
    response: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class IngressReservation:
    state: Literal[
        "reserved",
        "accepted",
        "processing",
        "completed",
        "failed",
        "recovery_required",
    ]
    generation: int


@dataclass(frozen=True)
class IngressRecord:
    channel: str
    account_id: str
    event_id: str
    frame_sha256: str
    state: str
    generation: int
    payload: Mapping[str, Any]
    event: CanonicalInboundEvent
    principal: Principal
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class GatewayEventRecord:
    seq: int
    event: str
    payload: Mapping[str, Any]
    created_at: float


@dataclass(frozen=True)
class GatewayEventReplay:
    events: tuple[GatewayEventRecord, ...]
    current_cursor: int
    resync_required: bool


@dataclass(frozen=True)
class OutboundRecord:
    outbound_id: str
    state: str
    generation: int
    envelope: Mapping[str, Any]
    provider_message_id: str | None
    error_code: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    account: ChannelAccountRef
    conversation: ConversationRef
    writer_generation: int
    mode: str
    debug: bool
    event_cursor: int
    active_run_id: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    session_id: str
    input_fingerprint: str
    state: RunState
    generation: int
    result: Mapping[str, Any] | None
    error_code: str | None
    recovery_from_state: str | None
    created_at: float
    started_at: float | None
    abort_requested_at: float | None
    finished_at: float | None
    updated_at: float


@dataclass(frozen=True)
class ApprovalRecord:
    request: ApprovalRequest
    status: Literal["pending", "resolved", "expired"]
    challenge: str | None = field(repr=False)
    decision_id: str | None = None
    accepted: bool | None = None
    decided_at: float | None = None
    generation: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class AuthorizationDecisionRecord:
    decision: AuthorizationDecision
    generation: int
    observed_at: float


class GatewayInstanceLease:
    """Hold one process-wide advisory lock until the Gateway host closes."""

    def __init__(self, descriptor: int) -> None:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("descriptor must be an open file descriptor")
        self._descriptor: int | None = descriptor

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            os.close(descriptor)
        except OSError as exc:
            raise GatewayStateError("Gateway instance lease could not be released") from exc


class GatewayStateStore:
    """Fail-closed state owner for one Bot instance Gateway.

    Every mutation carries the generation returned by ``acquire_writer_generation``.
    Acquiring a new generation fences previous writers and marks any possibly sent,
    unacknowledged outbound as ``delivery_unknown``. The trusted anchor and every
    state subdirectory must be private to the service UID; SQLite path reopening is
    not an isolation boundary against another malicious process running as that UID.
    """

    def __init__(
        self,
        root: Path,
        *,
        database_name: str = "gateway.sqlite3",
        trusted_anchor: Path | None = None,
    ) -> None:
        if Path(database_name).name != database_name or database_name in {"", ".", ".."}:
            raise ValueError("database_name must be one plain filename")
        self.root = Path(os.path.abspath(os.fspath(root)))
        configured_anchor = trusted_anchor if trusted_anchor is not None else self.root.parent
        self.trusted_anchor = Path(os.path.abspath(os.fspath(configured_anchor)))
        self.database_path = self.root / database_name
        _ensure_private_root(self.root, trusted_anchor=self.trusted_anchor)
        _ensure_private_database_file(self.database_path)
        self._initialize_schema()

    def acquire_instance_lease(self) -> GatewayInstanceLease:
        """Acquire this state root's non-blocking singleton process lease."""

        _validate_private_root(self.root, trusted_anchor=self.trusted_anchor)
        return _acquire_instance_lease(self.root / _INSTANCE_LEASE_FILENAME)

    def acquire_writer_generation(self, *, now: float | None = None) -> int:
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT value FROM gateway_meta WHERE key = 'writer_generation'"
            ).fetchone()
            current = int(row[0]) if row is not None else 0
            generation = current + 1
            connection.execute(
                "INSERT INTO gateway_meta(key, value) VALUES('writer_generation', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(generation),),
            )
            connection.execute(
                "UPDATE ingress SET state = 'recovery_required', updated_at = ? "
                "WHERE state = 'processing' AND generation < ?",
                (observed_at, generation),
            )
            connection.execute(
                "UPDATE idempotency SET state = 'recovery_required' "
                "WHERE state = 'pending' AND generation < ?",
                (generation,),
            )
            connection.execute(
                "UPDATE runs SET recovery_from_state = state, "
                "state = 'recovery_required', generation = ?, updated_at = ? "
                "WHERE state IN ('accepted', 'running', 'abort_requested') "
                "AND generation < ?",
                (generation, observed_at, generation),
            )
            connection.execute(
                "UPDATE sessions SET writer_generation = ? WHERE writer_generation < ?",
                (generation, generation),
            )
            not_submitted = connection.execute(
                "SELECT outbound_id FROM outbox "
                "WHERE state = 'pending' AND generation < ?",
                (generation,),
            ).fetchall()
            connection.execute(
                "UPDATE outbox SET state = 'failed', generation = ?, error_code = ?, "
                "updated_at = ? WHERE state = 'pending' AND generation < ?",
                (
                    generation,
                    "gateway_restarted_before_submission",
                    observed_at,
                    generation,
                ),
            )
            for row in not_submitted:
                self._insert_delivery_receipt(
                    connection,
                    receipt_id=_automatic_receipt_id(
                        str(row[0]), "failed", generation, observed_at
                    ),
                    outbound_id=str(row[0]),
                    stage="failed",
                    observed_at=observed_at,
                    provider_message_id=None,
                    error_code="gateway_restarted_before_submission",
                    detail={},
                )
            uncertain = connection.execute(
                "SELECT outbound_id FROM outbox "
                "WHERE state IN ('submitting', 'provider_submitted') AND generation < ?",
                (generation,),
            ).fetchall()
            connection.execute(
                "UPDATE outbox SET state = 'delivery_unknown', generation = ?, "
                "error_code = ?, updated_at = ? "
                "WHERE state IN ('submitting', 'provider_submitted') AND generation < ?",
                (
                    generation,
                    "gateway_restarted_before_ack",
                    observed_at,
                    generation,
                ),
            )
            for row in uncertain:
                self._insert_delivery_receipt(
                    connection,
                    receipt_id=_automatic_receipt_id(
                        str(row[0]), "delivery_unknown", generation, observed_at
                    ),
                    outbound_id=str(row[0]),
                    stage="delivery_unknown",
                    observed_at=observed_at,
                    provider_message_id=None,
                    error_code="gateway_restarted_before_ack",
                    detail={},
                )
        return generation

    def current_writer_generation(self) -> int:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT value FROM gateway_meta WHERE key = 'writer_generation'"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def assert_writer_generation(self, generation: int) -> None:
        """Fail closed even for read-only duplicate paths owned by a stale runtime."""

        with self._read_connection() as connection:
            self._assert_generation(connection, generation)

    def reserve_idempotency(
        self,
        *,
        generation: int,
        client_id: str,
        method: str,
        key: str,
        request_fingerprint: str,
        now: float | None = None,
        ttl_seconds: float = 24 * 60 * 60,
    ) -> IdempotencyReservation:
        observed_at = _timestamp(now)
        if (
            type(ttl_seconds) not in {int, float}
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be positive")
        _required_identity(client_id, "client_id")
        _required_identity(method, "method")
        _required_identity(key, "idempotency key", max_chars=256)
        _require_sha256(request_fingerprint, "request_fingerprint")
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            connection.execute(
                "DELETE FROM idempotency "
                "WHERE client_id = ? AND method = ? AND idempotency_key = ? "
                "AND state = 'completed' AND expires_at <= ?",
                (client_id, method, key, observed_at),
            )
            row = connection.execute(
                "SELECT request_fingerprint, state, response_json, generation "
                "FROM idempotency WHERE client_id = ? AND method = ? "
                "AND idempotency_key = ?",
                (client_id, method, key),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO idempotency("
                    "client_id, method, idempotency_key, request_fingerprint, state, "
                    "response_json, generation, created_at, expires_at"
                    ") VALUES(?, ?, ?, ?, 'pending', NULL, ?, ?, ?)",
                    (
                        client_id,
                        method,
                        key,
                        request_fingerprint,
                        generation,
                        observed_at,
                        observed_at + ttl_seconds,
                    ),
                )
                return IdempotencyReservation(state="reserved", generation=generation)
            if str(row[0]) != request_fingerprint:
                raise IdempotencyConflict("idempotency key is already bound to a different request")
            state = str(row[1])
            response = _json_object(str(row[2])) if row[2] is not None else None
            return IdempotencyReservation(
                state=cast(
                    Literal["pending", "completed", "recovery_required"],
                    state,
                ),
                generation=int(row[3]),
                response=response,
            )

    def resolve_idempotency_recovery(
        self,
        *,
        generation: int,
        client_id: str,
        method: str,
        key: str,
        request_fingerprint: str,
        resolution: Literal["retry", "fail"],
        terminal_response: Mapping[str, Any] | None = None,
        now: float | None = None,
        ttl_seconds: float = 24 * 60 * 60,
    ) -> IdempotencyReservation:
        """Explicitly retry or terminate a mutation interrupted by Gateway restart."""

        observed_at = _timestamp(now)
        if (
            type(ttl_seconds) not in {int, float}
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be positive")
        _required_identity(client_id, "client_id")
        _required_identity(method, "method")
        _required_identity(key, "idempotency key", max_chars=256)
        _require_sha256(request_fingerprint, "request_fingerprint")
        if resolution == "retry":
            if terminal_response is not None:
                raise ValueError("retry recovery cannot include a terminal response")
            next_state = "pending"
            response_json = None
        elif resolution == "fail":
            if terminal_response is None:
                raise ValueError("fail recovery requires a terminal response")
            next_state = "completed"
            response_json = _json_dump(terminal_response)
        else:
            raise ValueError("resolution must be retry or fail")

        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            row = connection.execute(
                "SELECT request_fingerprint, state FROM idempotency "
                "WHERE client_id = ? AND method = ? AND idempotency_key = ?",
                (client_id, method, key),
            ).fetchone()
            if row is None or str(row[1]) != "recovery_required":
                raise GatewayStateError("idempotency request does not require recovery")
            if str(row[0]) != request_fingerprint:
                raise IdempotencyConflict("idempotency key is already bound to a different request")
            connection.execute(
                "UPDATE idempotency SET state = ?, response_json = ?, generation = ?, "
                "expires_at = ? WHERE client_id = ? AND method = ? AND idempotency_key = ?",
                (
                    next_state,
                    response_json,
                    generation,
                    observed_at + ttl_seconds,
                    client_id,
                    method,
                    key,
                ),
            )
        return IdempotencyReservation(
            state="reserved" if resolution == "retry" else "completed",
            generation=generation,
            response=dict(terminal_response) if terminal_response is not None else None,
        )

    def complete_idempotency(
        self,
        *,
        generation: int,
        client_id: str,
        method: str,
        key: str,
        request_fingerprint: str,
        response: Mapping[str, Any],
    ) -> None:
        response_json = _json_dump(response)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            row = connection.execute(
                "SELECT request_fingerprint, state, response_json, generation "
                "FROM idempotency WHERE client_id = ? AND method = ? "
                "AND idempotency_key = ?",
                (client_id, method, key),
            ).fetchone()
            if row is None:
                raise GatewayStateError("idempotency reservation does not exist")
            if str(row[0]) != request_fingerprint:
                raise IdempotencyConflict("idempotency key is already bound to a different request")
            if int(row[3]) != generation:
                raise StaleWriterGeneration(
                    "a previous Gateway generation owns this pending request"
                )
            if str(row[1]) == "completed":
                if str(row[2]) != response_json:
                    raise IdempotencyConflict("completed idempotency response cannot be replaced")
                return
            connection.execute(
                "UPDATE idempotency SET state = 'completed', response_json = ? "
                "WHERE client_id = ? AND method = ? AND idempotency_key = ?",
                (response_json, client_id, method, key),
            )

    def create_session(
        self,
        *,
        generation: int,
        session_id: str,
        account: ChannelAccountRef,
        conversation: ConversationRef,
        mode: str = "default",
        debug: bool = False,
        event_cursor: int = 0,
        now: float | None = None,
    ) -> SessionRecord:
        observed_at = _timestamp(now)
        _validate_session_fields(
            session_id=session_id,
            account=account,
            conversation=conversation,
            mode=mode,
            debug=debug,
            event_cursor=event_cursor,
        )
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            existing_row = connection.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing_row is not None:
                existing = _session_record(session_id, existing_row)
                if (
                    existing.account != account
                    or existing.conversation != conversation
                    or existing.mode != mode
                    or existing.debug != debug
                    or existing.event_cursor != event_cursor
                ):
                    raise SessionConflict("session identity is already bound to different state")
                return existing
            conversation_row = connection.execute(
                "SELECT session_id FROM sessions WHERE channel = ? AND account_id = ? "
                "AND conversation_kind = ? AND conversation_id = ?",
                (
                    account.channel,
                    account.account_id,
                    conversation.kind,
                    conversation.conversation_id,
                ),
            ).fetchone()
            if conversation_row is not None:
                raise SessionConflict("Gateway conversation is already bound to another session")
            try:
                connection.execute(
                    "INSERT INTO sessions("
                    "session_id, channel, account_id, conversation_kind, conversation_id, "
                    "writer_generation, mode, debug, event_cursor, active_run_id, "
                    "created_at, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        session_id,
                        account.channel,
                        account.account_id,
                        conversation.kind,
                        conversation.conversation_id,
                        generation,
                        mode,
                        int(debug),
                        event_cursor,
                        observed_at,
                        observed_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SessionConflict("Gateway session binding already exists") from exc
            return SessionRecord(
                session_id=session_id,
                account=account,
                conversation=conversation,
                writer_generation=generation,
                mode=mode,
                debug=debug,
                event_cursor=event_cursor,
                active_run_id=None,
                created_at=observed_at,
                updated_at=observed_at,
            )

    def get_session(self, session_id: str) -> SessionRecord | None:
        _required_identity(session_id, "session_id", max_chars=256)
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return _session_record(session_id, row) if row is not None else None

    def find_session_by_conversation(
        self,
        *,
        account: ChannelAccountRef,
        conversation: ConversationRef,
    ) -> SessionRecord | None:
        _validate_account_conversation(account, conversation)
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT session_id, {_SESSION_COLUMNS} FROM sessions "
                "WHERE channel = ? AND account_id = ? AND conversation_kind = ? "
                "AND conversation_id = ?",
                (
                    account.channel,
                    account.account_id,
                    conversation.kind,
                    conversation.conversation_id,
                ),
            ).fetchone()
        return _session_record(str(row[0]), row[1:]) if row is not None else None

    def list_sessions(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[SessionRecord, ...]:
        _validate_page(offset=offset, limit=limit)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT session_id, {_SESSION_COLUMNS} FROM sessions "
                "ORDER BY created_at ASC, session_id ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return tuple(_session_record(str(row[0]), row[1:]) for row in rows)

    def patch_session(
        self,
        *,
        generation: int,
        session_id: str,
        mode: str | None = None,
        debug: bool | None = None,
        event_cursor: int | None = None,
        now: float | None = None,
    ) -> SessionRecord:
        if mode is None and debug is None and event_cursor is None:
            raise ValueError("session patch requires mode, debug, or event_cursor")
        _required_identity(session_id, "session_id", max_chars=256)
        if mode is not None:
            _validate_session_mode(mode)
        if debug is not None and type(debug) is not bool:
            raise ValueError("session debug must be a boolean")
        if event_cursor is not None and (type(event_cursor) is not int or event_cursor < 0):
            raise ValueError("session event_cursor must be a non-negative integer")
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            current = self._require_session(connection, session_id)
            if event_cursor is not None and event_cursor < current.event_cursor:
                raise SessionConflict("Gateway session event cursor cannot move backwards")
            next_mode = current.mode if mode is None else mode
            next_debug = current.debug if debug is None else debug
            next_cursor = current.event_cursor if event_cursor is None else event_cursor
            connection.execute(
                "UPDATE sessions SET mode = ?, debug = ?, event_cursor = ?, "
                "writer_generation = ?, updated_at = ? WHERE session_id = ?",
                (
                    next_mode,
                    int(next_debug),
                    next_cursor,
                    generation,
                    observed_at,
                    session_id,
                ),
            )
            return SessionRecord(
                session_id=current.session_id,
                account=current.account,
                conversation=current.conversation,
                writer_generation=generation,
                mode=next_mode,
                debug=next_debug,
                event_cursor=next_cursor,
                active_run_id=current.active_run_id,
                created_at=current.created_at,
                updated_at=observed_at,
            )

    def begin_run(
        self,
        *,
        generation: int,
        session_id: str,
        run_id: str,
        input_fingerprint: str,
        now: float | None = None,
    ) -> RunRecord:
        _required_identity(session_id, "session_id", max_chars=256)
        _required_identity(run_id, "run_id", max_chars=256)
        _require_sha256(input_fingerprint, "input_fingerprint")
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            session = self._require_session(connection, session_id)
            existing_row = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing_row is not None:
                existing = _run_record(run_id, existing_row)
                if (
                    existing.session_id != session_id
                    or existing.input_fingerprint != input_fingerprint
                ):
                    raise RunConflict("run identity is already bound to different input")
                if existing.state in ACTIVE_RUN_STATES:
                    _assert_session_run_binding(session, run_id)
                return existing
            if session.active_run_id is not None:
                raise RunConflict("Gateway session already has an active run")
            try:
                connection.execute(
                    "INSERT INTO runs("
                    "run_id, session_id, input_fingerprint, state, generation, result_json, "
                    "error_code, recovery_from_state, created_at, started_at, "
                    "abort_requested_at, finished_at, updated_at"
                    ") VALUES(?, ?, ?, 'accepted', ?, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?)",
                    (
                        run_id,
                        session_id,
                        input_fingerprint,
                        generation,
                        observed_at,
                        observed_at,
                    ),
                )
                cursor = connection.execute(
                    "UPDATE sessions SET active_run_id = ?, writer_generation = ?, updated_at = ? "
                    "WHERE session_id = ? AND active_run_id IS NULL",
                    (run_id, generation, observed_at, session_id),
                )
            except sqlite3.IntegrityError as exc:
                raise RunConflict("Gateway session already has an active run") from exc
            if cursor.rowcount != 1:
                raise RunConflict("Gateway session already has an active run")
            return RunRecord(
                run_id=run_id,
                session_id=session_id,
                input_fingerprint=input_fingerprint,
                state="accepted",
                generation=generation,
                result=None,
                error_code=None,
                recovery_from_state=None,
                created_at=observed_at,
                started_at=None,
                abort_requested_at=None,
                finished_at=None,
                updated_at=observed_at,
            )

    def start_run(
        self,
        *,
        generation: int,
        session_id: str,
        run_id: str,
        now: float | None = None,
    ) -> RunRecord:
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            session = self._require_session(connection, session_id)
            current = self._require_run(connection, run_id)
            _assert_run_session(current, session_id)
            _assert_session_run_binding(session, run_id)
            if current.state == "running":
                return current
            if current.state != "accepted":
                raise RunConflict("only an accepted run can start")
            connection.execute(
                "UPDATE runs SET state = 'running', generation = ?, started_at = ?, "
                "updated_at = ? WHERE run_id = ? AND state = 'accepted'",
                (generation, observed_at, observed_at, run_id),
            )
            return _replace_run_state(
                current,
                state="running",
                generation=generation,
                started_at=observed_at,
                updated_at=observed_at,
            )

    def request_abort(
        self,
        *,
        generation: int,
        session_id: str,
        run_id: str,
        now: float | None = None,
    ) -> RunRecord:
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            current = self._require_run(connection, run_id)
            _assert_run_session(current, session_id)
            if current.state in TERMINAL_RUN_STATES:
                return current
            session = self._require_session(connection, session_id)
            _assert_session_run_binding(session, run_id)
            if current.state == "recovery_required":
                raise RunConflict("recovery-required run must be explicitly resolved")
            if current.state == "abort_requested":
                return current
            if current.state not in {"accepted", "running"}:
                raise RunConflict("run cannot accept an abort request from its current state")
            connection.execute(
                "UPDATE runs SET state = 'abort_requested', generation = ?, "
                "abort_requested_at = ?, updated_at = ? WHERE run_id = ? "
                "AND state IN ('accepted', 'running')",
                (generation, observed_at, observed_at, run_id),
            )
            return _replace_run_state(
                current,
                state="abort_requested",
                generation=generation,
                abort_requested_at=observed_at,
                updated_at=observed_at,
            )

    def finish_run(
        self,
        *,
        generation: int,
        session_id: str,
        run_id: str,
        outcome: RunOutcome,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        worker_stop_reason: Literal["cancelled"] | None = None,
        now: float | None = None,
    ) -> RunRecord:
        result_json = _validate_run_terminal_payload(
            outcome=outcome,
            result=result,
            error_code=error_code,
            worker_stop_reason=worker_stop_reason,
        )
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            current = self._require_run(connection, run_id)
            _assert_run_session(current, session_id)
            if current.state in TERMINAL_RUN_STATES:
                _assert_terminal_run_replay(
                    current,
                    outcome=outcome,
                    result=result,
                    error_code=error_code,
                )
                return current
            if current.state == "recovery_required":
                raise RunConflict("recovery-required run requires explicit resolution")
            if outcome == "completed" and current.state not in {
                "running",
                "abort_requested",
            }:
                raise RunConflict("only a started run can complete")
            if outcome == "failed" and current.state not in {
                "accepted",
                "running",
                "abort_requested",
            }:
                raise RunConflict("run cannot fail from its current state")
            if outcome == "aborted" and current.state != "abort_requested":
                raise RunConflict("run can become aborted only after an abort request")
            session = self._require_session(connection, session_id)
            _assert_session_run_binding(session, run_id)
            connection.execute(
                "UPDATE runs SET state = ?, generation = ?, result_json = ?, error_code = ?, "
                "finished_at = ?, updated_at = ? WHERE run_id = ?",
                (
                    outcome,
                    generation,
                    result_json,
                    error_code,
                    observed_at,
                    observed_at,
                    run_id,
                ),
            )
            self._clear_active_run(
                connection,
                generation=generation,
                session_id=session_id,
                run_id=run_id,
                observed_at=observed_at,
            )
            return _replace_run_state(
                current,
                state=outcome,
                generation=generation,
                result=dict(result) if result is not None else None,
                error_code=error_code,
                finished_at=observed_at,
                updated_at=observed_at,
            )

    def resolve_run_recovery(
        self,
        *,
        generation: int,
        session_id: str,
        run_id: str,
        outcome: RunOutcome,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        worker_stop_reason: Literal["cancelled"] | None = None,
        now: float | None = None,
    ) -> RunRecord:
        result_json = _validate_run_terminal_payload(
            outcome=outcome,
            result=result,
            error_code=error_code,
            worker_stop_reason=worker_stop_reason,
        )
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            current = self._require_run(connection, run_id)
            _assert_run_session(current, session_id)
            if current.state in TERMINAL_RUN_STATES:
                _assert_terminal_run_replay(
                    current,
                    outcome=outcome,
                    result=result,
                    error_code=error_code,
                )
                return current
            if current.state != "recovery_required":
                raise RunConflict("run does not require recovery")
            if outcome == "completed" and current.recovery_from_state not in {
                "running",
                "abort_requested",
            }:
                raise RunConflict("an unstarted recovered run cannot be completed")
            if outcome == "aborted" and current.recovery_from_state != "abort_requested":
                raise RunConflict("recovered run has no durable abort request")
            session = self._require_session(connection, session_id)
            _assert_session_run_binding(session, run_id)
            connection.execute(
                "UPDATE runs SET state = ?, generation = ?, result_json = ?, error_code = ?, "
                "finished_at = ?, updated_at = ? WHERE run_id = ? "
                "AND state = 'recovery_required'",
                (
                    outcome,
                    generation,
                    result_json,
                    error_code,
                    observed_at,
                    observed_at,
                    run_id,
                ),
            )
            self._clear_active_run(
                connection,
                generation=generation,
                session_id=session_id,
                run_id=run_id,
                observed_at=observed_at,
            )
            return _replace_run_state(
                current,
                state=outcome,
                generation=generation,
                result=dict(result) if result is not None else None,
                error_code=error_code,
                finished_at=observed_at,
                updated_at=observed_at,
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        _required_identity(run_id, "run_id", max_chars=256)
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return _run_record(run_id, row) if row is not None else None

    def latest_run_for_session(self, session_id: str) -> RunRecord | None:
        _required_identity(session_id, "session_id", max_chars=256)
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT run_id, {_RUN_COLUMNS} FROM runs WHERE session_id = ? "
                "ORDER BY created_at DESC, run_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return _run_record(str(row[0]), row[1:]) if row is not None else None

    def list_active_runs(
        self,
        *,
        session_id: str | None = None,
        limit: int = 100,
    ) -> tuple[RunRecord, ...]:
        if type(limit) is not int or limit < 1 or limit > 1000:
            raise ValueError("run list limit must be between 1 and 1000")
        params: list[Any] = []
        session_filter = ""
        if session_id is not None:
            _required_identity(session_id, "session_id", max_chars=256)
            session_filter = "AND session_id = ? "
            params.append(session_id)
        params.append(limit)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT run_id, {_RUN_COLUMNS} FROM runs "
                "WHERE state IN ('accepted', 'running', 'abort_requested', "
                f"'recovery_required') {session_filter}"
                "ORDER BY created_at ASC, run_id ASC LIMIT ?",
                tuple(params),
            ).fetchall()
        return tuple(_run_record(str(row[0]), row[1:]) for row in rows)

    def create_approval(
        self,
        *,
        generation: int,
        request: ApprovalRequest,
        challenge: str,
        now: float | None = None,
    ) -> bool:
        """Persist an approval and its opaque prompt before notifying any client."""

        observed_at = _timestamp(now)
        _validate_approval_request(request)
        _validate_approval_challenge(challenge)
        if not hmac.compare_digest(
            _challenge_digest(challenge),
            request.challenge_digest,
        ):
            raise ValueError("approval challenge does not match its digest")
        if request.expires_at <= observed_at:
            raise ValueError("approval must expire after it is created")
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            self._require_session(connection, request.session_id)
            if request.run_id is not None:
                run = self._require_run(connection, request.run_id)
                _assert_run_session(run, request.session_id)
                if run.state not in ACTIVE_RUN_STATES:
                    raise ApprovalConflict("approval run is no longer active")
            row = connection.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE approval_id = ?",
                (request.approval_id,),
            ).fetchone()
            if row is not None:
                existing = _approval_record(request.approval_id, row, now=observed_at)
                if existing.request == request and hmac.compare_digest(
                    existing.challenge or "",
                    challenge,
                ):
                    return False
                raise ApprovalConflict(
                    "approval identity is already bound to different request facts"
                )
            try:
                connection.execute(
                    "INSERT INTO approvals("
                    "approval_id, session_id, run_id, operation, target, params_digest, "
                    "actor_ref, conversation_ref, policy_version, challenge_digest, "
                    "challenge, expires_at, state, decision_id, accepted, decided_at, "
                    "generation, created_at, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, "
                    "NULL, NULL, ?, ?, ?)",
                    (
                        request.approval_id,
                        request.session_id,
                        request.run_id,
                        request.operation,
                        request.target,
                        request.params_digest,
                        request.actor_ref,
                        request.conversation_ref,
                        request.policy_version,
                        request.challenge_digest,
                        challenge,
                        request.expires_at,
                        generation,
                        observed_at,
                        observed_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ApprovalConflict("approval binding is invalid") from exc
        return True

    def get_approval(
        self,
        approval_id: str,
        *,
        actor_ref: str,
        conversation_ref: str,
        session_id: str,
        now: float | None = None,
    ) -> ApprovalRecord | None:
        """Return one approval only through its exact actor/conversation/session binding."""

        observed_at = _timestamp(now)
        _required_identity(approval_id, "approval_id", max_chars=256)
        _validate_approval_access(
            actor_ref=actor_ref,
            conversation_ref=conversation_ref,
            session_id=session_id,
        )
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM approvals "
                "WHERE approval_id = ? AND actor_ref = ? AND conversation_ref = ? "
                "AND session_id = ?",
                (approval_id, actor_ref, conversation_ref, session_id),
            ).fetchone()
        return (
            _approval_record(approval_id, row, now=observed_at)
            if row is not None
            else None
        )

    def list_approvals(
        self,
        *,
        actor_ref: str,
        conversation_ref: str,
        session_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
        now: float | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        """List only approvals visible to one exact trusted actor and conversation."""

        observed_at = _timestamp(now)
        _validate_approval_access(
            actor_ref=actor_ref,
            conversation_ref=conversation_ref,
            session_id=session_id,
        )
        _validate_page(offset=offset, limit=limit)
        params: list[Any] = [actor_ref, conversation_ref]
        session_filter = ""
        if session_id is not None:
            session_filter = "AND session_id = ? "
            params.append(session_id)
        params.extend((limit, offset))
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT approval_id, {_APPROVAL_COLUMNS} FROM approvals "
                "WHERE actor_ref = ? AND conversation_ref = ? "
                f"{session_filter}ORDER BY created_at ASC, approval_id ASC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return tuple(
            _approval_record(str(row[0]), row[1:], now=observed_at) for row in rows
        )

    def expire_approvals(
        self,
        *,
        generation: int,
        now: float | None = None,
    ) -> int:
        """Make expiry durable and erase no-longer-usable plaintext challenges."""

        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cursor = connection.execute(
                "UPDATE approvals SET state = 'expired', challenge = '', generation = ?, "
                "updated_at = ? WHERE state = 'pending' AND expires_at <= ?",
                (generation, observed_at, observed_at),
            )
            return cursor.rowcount

    def record_authorization_decision(
        self,
        *,
        generation: int,
        decision: AuthorizationDecision,
        retain_last: int = 10_000,
        now: float | None = None,
    ) -> AuthorizationDecisionRecord:
        """Persist an exact policy result before the authorized or denied side effect."""

        _validate_authorization_decision(decision)
        if type(retain_last) is not int or retain_last < 1:
            raise ValueError("authorization decision retention must be positive")
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            existing = connection.execute(
                "SELECT request_id, request_digest, allowed, code, policy_version, "
                "actor_ref, generation, observed_at FROM authorization_decisions "
                "WHERE decision_id = ?",
                (decision.decision_id,),
            ).fetchone()
            if existing is not None:
                record = _authorization_decision_record(
                    decision.decision_id,
                    existing,
                )
                if record.decision != decision:
                    raise GatewayStateError(
                        "authorization decision identity is already bound to another result"
                    )
                return record
            connection.execute(
                "INSERT INTO authorization_decisions("
                "decision_id, request_id, request_digest, allowed, code, policy_version, "
                "actor_ref, generation, observed_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.decision_id,
                    decision.request_id,
                    decision.request_digest,
                    int(decision.allowed),
                    decision.code,
                    decision.policy_version,
                    decision.actor_ref,
                    generation,
                    observed_at,
                ),
            )
            connection.execute(
                "DELETE FROM authorization_decisions WHERE decision_id IN ("
                "SELECT decision_id FROM authorization_decisions "
                "ORDER BY observed_at DESC, rowid DESC LIMIT -1 OFFSET ?"
                ")",
                (retain_last,),
            )
        return AuthorizationDecisionRecord(
            decision=decision,
            generation=generation,
            observed_at=observed_at,
        )

    def list_authorization_decisions(
        self,
        *,
        actor_ref: str | None = None,
        limit: int = 100,
    ) -> tuple[AuthorizationDecisionRecord, ...]:
        """Read the newest bounded policy receipts, optionally for one trusted actor."""

        if actor_ref is not None:
            _required_identity(actor_ref, "authorization actor_ref", max_chars=512)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("authorization decision limit must be between 1 and 1000")
        where = "" if actor_ref is None else "WHERE actor_ref = ? "
        params: tuple[Any, ...] = (limit,) if actor_ref is None else (actor_ref, limit)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT decision_id, request_id, request_digest, allowed, code, "
                "policy_version, actor_ref, generation, observed_at "
                f"FROM authorization_decisions {where}"
                "ORDER BY observed_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return tuple(
            _authorization_decision_record(str(row[0]), row[1:]) for row in rows
        )

    def _get_approval_request(self, approval_id: str) -> ApprovalRequest | None:
        _required_identity(approval_id, "approval_id", max_chars=256)
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return (
            _approval_record(approval_id, row, now=0.0).request
            if row is not None
            else None
        )

    def resolve_approval_once(
        self,
        *,
        generation: int,
        request: ApprovalRequest,
        resolution: ApprovalResolution,
        decision_id: str,
        decided_at: float,
    ) -> bool:
        """Atomically consume an approval only if every immutable binding still matches."""

        observed_at = _timestamp(decided_at)
        _validate_approval_request(request)
        _validate_approval_resolution(resolution)
        _required_identity(decision_id, "decision_id", max_chars=256)
        if (
            resolution.approval_id != request.approval_id
            or resolution.actor_ref != request.actor_ref
            or resolution.conversation_ref != request.conversation_ref
            or resolution.params_digest != request.params_digest
            or resolution.policy_version != request.policy_version
            or not hmac.compare_digest(
                _challenge_digest(resolution.challenge),
                request.challenge_digest,
            )
        ):
            return False
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cursor = connection.execute(
                "UPDATE approvals SET state = 'resolved', challenge = '', decision_id = ?, "
                "accepted = ?, decided_at = ?, generation = ?, updated_at = ? "
                "WHERE approval_id = ? AND session_id = ? AND run_id IS ? "
                "AND operation = ? AND target = ? AND params_digest = ? "
                "AND actor_ref = ? AND conversation_ref = ? AND policy_version = ? "
                "AND challenge_digest = ? AND challenge = ? AND expires_at = ? "
                "AND state = 'pending' AND expires_at > ?",
                (
                    decision_id,
                    int(resolution.accepted),
                    observed_at,
                    generation,
                    observed_at,
                    request.approval_id,
                    request.session_id,
                    request.run_id,
                    request.operation,
                    request.target,
                    request.params_digest,
                    resolution.actor_ref,
                    resolution.conversation_ref,
                    resolution.policy_version,
                    request.challenge_digest,
                    resolution.challenge,
                    request.expires_at,
                    observed_at,
                ),
            )
            return cursor.rowcount == 1

    def reserve_ingress(
        self,
        *,
        generation: int,
        event: CanonicalInboundEvent,
        principal: Principal,
        now: float | None = None,
    ) -> IngressReservation:
        observed_at = _timestamp(now)
        evidence = event.evidence
        _required_identity(evidence.account.channel, "channel")
        _required_identity(evidence.account.account_id, "account_id")
        _required_identity(evidence.event_id, "event_id", max_chars=256)
        _require_sha256(evidence.frame_sha256, "frame_sha256")
        _validate_ingress_principal(principal, event)
        payload_json = _json_dump(asdict(event))
        principal_json = _json_dump(asdict(principal))
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            row = connection.execute(
                "SELECT frame_sha256, state, generation, principal_json FROM ingress "
                "WHERE channel = ? AND account_id = ? AND event_id = ?",
                (
                    evidence.account.channel,
                    evidence.account.account_id,
                    evidence.event_id,
                ),
            ).fetchone()
            if row is not None:
                if str(row[0]) != evidence.frame_sha256:
                    raise IngressConflict(
                        "provider event identity is already bound to different evidence"
                    )
                if not hmac.compare_digest(str(row[3]), principal_json):
                    raise IngressConflict(
                        "provider event identity is already bound to another principal"
                    )
                return IngressReservation(
                    state=cast(
                        Literal[
                            "accepted",
                            "processing",
                            "completed",
                            "failed",
                            "recovery_required",
                        ],
                        str(row[1]),
                    ),
                    generation=int(row[2]),
                )
            connection.execute(
                "INSERT INTO ingress("
                "channel, account_id, event_id, frame_sha256, payload_json, principal_json, state, "
                "generation, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?)",
                (
                    evidence.account.channel,
                    evidence.account.account_id,
                    evidence.event_id,
                    evidence.frame_sha256,
                    payload_json,
                    principal_json,
                    generation,
                    observed_at,
                    observed_at,
                ),
            )
            return IngressReservation(state="reserved", generation=generation)

    def claim_ingress(
        self,
        *,
        generation: int,
        channel: str,
        account_id: str,
        event_id: str,
        now: float | None = None,
    ) -> bool:
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cursor = connection.execute(
                "UPDATE ingress SET state = 'processing', generation = ?, updated_at = ? "
                "WHERE channel = ? AND account_id = ? AND event_id = ? "
                "AND state = 'accepted'",
                (generation, observed_at, channel, account_id, event_id),
            )
            return cursor.rowcount == 1

    def finish_ingress(
        self,
        *,
        generation: int,
        channel: str,
        account_id: str,
        event_id: str,
        succeeded: bool,
        retain_terminal: int = 10_000,
        now: float | None = None,
    ) -> None:
        _validate_ingress_retention(retain_terminal)
        observed_at = _timestamp(now)
        state = "completed" if succeeded else "failed"
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cursor = connection.execute(
                "UPDATE ingress SET state = ?, updated_at = ? "
                "WHERE channel = ? AND account_id = ? AND event_id = ? "
                "AND state = 'processing' AND generation = ?",
                (state, observed_at, channel, account_id, event_id, generation),
            )
            if cursor.rowcount != 1:
                raise GatewayStateError("ingress is not owned by the active generation")
            _prune_terminal_ingress_rows(
                connection,
                retain_last=retain_terminal,
            )

    def prune_terminal_ingress(
        self,
        *,
        generation: int,
        retain_last: int = 10_000,
    ) -> int:
        """Bound terminal intake evidence without deleting active or recovery records."""

        _validate_ingress_retention(retain_last)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            return _prune_terminal_ingress_rows(
                connection,
                retain_last=retain_last,
            )

    def get_ingress(
        self,
        *,
        channel: str,
        account_id: str,
        event_id: str,
    ) -> IngressRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT frame_sha256, state, generation, payload_json, principal_json, "
                "created_at, updated_at FROM ingress "
                "WHERE channel = ? AND account_id = ? AND event_id = ?",
                (channel, account_id, event_id),
            ).fetchone()
        return _ingress_record(channel, account_id, event_id, row) if row is not None else None

    def list_ingress(
        self,
        *,
        states: tuple[str, ...] = ("accepted", "recovery_required"),
        limit: int = 100,
    ) -> tuple[IngressRecord, ...]:
        if not states or any(state not in INGRESS_STATES for state in states):
            raise ValueError("states contains an invalid ingress state")
        if len(set(states)) != len(states):
            raise ValueError("states cannot contain duplicates")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        placeholders = ",".join("?" for _ in states)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT channel, account_id, event_id, frame_sha256, state, generation, "
                "payload_json, principal_json, created_at, updated_at FROM ingress "
                f"WHERE state IN ({placeholders}) ORDER BY created_at ASC LIMIT ?",
                (*states, limit),
            ).fetchall()
        return tuple(
            _ingress_record(str(row[0]), str(row[1]), str(row[2]), row[3:])
            for row in rows
        )

    def resolve_ingress_recovery(
        self,
        *,
        generation: int,
        channel: str,
        account_id: str,
        event_id: str,
        retry: bool,
        retain_terminal: int = 10_000,
        now: float | None = None,
    ) -> IngressRecord:
        """Explicitly retry or terminate an interrupted ingress; never auto-replay it."""

        _validate_ingress_retention(retain_terminal)
        observed_at = _timestamp(now)
        target = "accepted" if retry else "failed"
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cursor = connection.execute(
                "UPDATE ingress SET state = ?, generation = ?, updated_at = ? "
                "WHERE channel = ? AND account_id = ? AND event_id = ? "
                "AND state = 'recovery_required'",
                (
                    target,
                    generation,
                    observed_at,
                    channel,
                    account_id,
                    event_id,
                ),
            )
            if cursor.rowcount != 1:
                raise GatewayStateError("ingress does not require recovery")
            if not retry:
                _prune_terminal_ingress_rows(
                    connection,
                    retain_last=retain_terminal,
                )
        record = self.get_ingress(
            channel=channel,
            account_id=account_id,
            event_id=event_id,
        )
        if record is None:
            raise GatewayStateError("recovered ingress disappeared")
        return record

    def enqueue_outbound(
        self,
        *,
        generation: int,
        envelope: OutboundEnvelope,
    ) -> OutboundRecord:
        _required_identity(envelope.outbound_id, "outbound_id", max_chars=256)
        created_at = _timestamp(envelope.created_at)
        envelope_json = _json_dump(asdict(envelope))
        fingerprint = hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            row = connection.execute(
                "SELECT envelope_sha256, state, generation, envelope_json, "
                "provider_message_id, error_code, created_at, updated_at "
                "FROM outbox WHERE outbound_id = ?",
                (envelope.outbound_id,),
            ).fetchone()
            if row is not None:
                if str(row[0]) != fingerprint:
                    raise OutboundConflict(
                        "outbound identity is already bound to a different envelope"
                    )
                return _outbound_record(envelope.outbound_id, row[1:])
            connection.execute(
                "INSERT INTO outbox("
                "outbound_id, envelope_sha256, envelope_json, session_id, run_id, "
                "state, generation, "
                "provider_message_id, error_code, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, ?, ?)",
                (
                    envelope.outbound_id,
                    fingerprint,
                    envelope_json,
                    envelope.session_id,
                    envelope.run_id,
                    generation,
                    created_at,
                    created_at,
                ),
            )
            self._insert_delivery_receipt(
                connection,
                receipt_id=_automatic_receipt_id(
                    envelope.outbound_id,
                    "gateway_accepted",
                    generation,
                    created_at,
                ),
                outbound_id=envelope.outbound_id,
                stage="gateway_accepted",
                observed_at=created_at,
                provider_message_id=None,
                error_code=None,
                detail={},
            )
            return OutboundRecord(
                outbound_id=envelope.outbound_id,
                state="pending",
                generation=generation,
                envelope=_json_object(envelope_json),
                provider_message_id=None,
                error_code=None,
                created_at=created_at,
                updated_at=created_at,
            )

    def begin_outbound_submission(
        self,
        *,
        generation: int,
        outbound_id: str,
        now: float | None = None,
    ) -> None:
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cursor = connection.execute(
                "UPDATE outbox SET state = 'submitting', generation = ?, updated_at = ? "
                "WHERE outbound_id = ? AND state = 'pending'",
                (generation, observed_at, outbound_id),
            )
            if cursor.rowcount != 1:
                raise GatewayStateError("outbound is not pending")

    def mark_outbound_submitted(
        self,
        *,
        generation: int,
        outbound_id: str,
        now: float | None = None,
    ) -> None:
        observed_at = _timestamp(now)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cursor = connection.execute(
                "UPDATE outbox SET state = 'provider_submitted', updated_at = ? "
                "WHERE outbound_id = ? AND state = 'submitting' AND generation = ?",
                (observed_at, outbound_id, generation),
            )
            if cursor.rowcount != 1:
                raise GatewayStateError("outbound is not being submitted by this generation")
            self._insert_delivery_receipt(
                connection,
                receipt_id=_automatic_receipt_id(
                    outbound_id, "provider_submitted", generation, observed_at
                ),
                outbound_id=outbound_id,
                stage="provider_submitted",
                observed_at=observed_at,
                provider_message_id=None,
                error_code=None,
                detail={},
            )

    def acknowledge_outbound(
        self,
        *,
        generation: int,
        outbound_id: str,
        provider_message_id: str | None,
        now: float | None = None,
    ) -> None:
        observed_at = _timestamp(now)
        if provider_message_id is not None:
            _required_identity(provider_message_id, "provider_message_id", max_chars=256)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            row = connection.execute(
                "SELECT state, generation FROM outbox WHERE outbound_id = ?",
                (outbound_id,),
            ).fetchone()
            if row is None or int(row[1]) != generation:
                raise GatewayStateError("outbound is not owned by this generation")
            current = str(row[0])
            if current == "submitting":
                self._insert_delivery_receipt(
                    connection,
                    receipt_id=_automatic_receipt_id(
                        outbound_id, "provider_submitted", generation, observed_at
                    ),
                    outbound_id=outbound_id,
                    stage="provider_submitted",
                    observed_at=observed_at,
                    provider_message_id=None,
                    error_code=None,
                    detail={},
                )
            elif current != "provider_submitted":
                raise GatewayStateError("outbound has not entered provider submission")
            cursor = connection.execute(
                "UPDATE outbox SET state = 'provider_acknowledged', "
                "provider_message_id = ?, updated_at = ? "
                "WHERE outbound_id = ? AND state = ? AND generation = ?",
                (provider_message_id, observed_at, outbound_id, current, generation),
            )
            if cursor.rowcount != 1:
                raise GatewayStateError("outbound has not been submitted by this generation")
            self._insert_delivery_receipt(
                connection,
                receipt_id=_automatic_receipt_id(
                    outbound_id, "provider_acknowledged", generation, observed_at
                ),
                outbound_id=outbound_id,
                stage="provider_acknowledged",
                observed_at=observed_at,
                provider_message_id=provider_message_id,
                error_code=None,
                detail={},
            )

    def fail_outbound(
        self,
        *,
        generation: int,
        outbound_id: str,
        error_code: str,
        definitely_not_submitted: bool,
        now: float | None = None,
    ) -> None:
        observed_at = _timestamp(now)
        _required_identity(error_code, "error_code", max_chars=256)
        target = "failed" if definitely_not_submitted else "delivery_unknown"
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            row = connection.execute(
                "SELECT state, generation FROM outbox WHERE outbound_id = ?",
                (outbound_id,),
            ).fetchone()
            if row is None or int(row[1]) != generation:
                raise GatewayStateError("outbound is not owned by this generation")
            current = str(row[0])
            if definitely_not_submitted and current not in {"pending", "submitting"}:
                raise GatewayStateError("submitted outbound cannot be marked definitely failed")
            if not definitely_not_submitted and current not in {
                "submitting",
                "provider_submitted",
            }:
                raise GatewayStateError("only an uncertain submission can become delivery_unknown")
            connection.execute(
                "UPDATE outbox SET state = ?, error_code = ?, updated_at = ? WHERE outbound_id = ?",
                (target, error_code, observed_at, outbound_id),
            )
            self._insert_delivery_receipt(
                connection,
                receipt_id=_automatic_receipt_id(outbound_id, target, generation, observed_at),
                outbound_id=outbound_id,
                stage=target,
                observed_at=observed_at,
                provider_message_id=None,
                error_code=error_code,
                detail={},
            )

    def get_outbound(self, outbound_id: str) -> OutboundRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT state, generation, envelope_json, provider_message_id, "
                "error_code, created_at, updated_at FROM outbox WHERE outbound_id = ?",
                (outbound_id,),
            ).fetchone()
        return _outbound_record(outbound_id, row) if row is not None else None

    def find_outbound_deliveries(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        outbound_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OutboundRecord, ...]:
        """Find exact durable delivery state through one visible session binding."""

        _required_identity(session_id, "delivery session_id", max_chars=256)
        if (run_id is None) == (outbound_id is None):
            raise ValueError("exactly one delivery locator is required")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("delivery query limit must be between 1 and 100")
        if run_id is not None:
            _required_identity(run_id, "delivery run_id", max_chars=256)
            where = "session_id = ? AND run_id = ?"
            params: tuple[Any, ...] = (session_id, run_id, limit)
        else:
            assert outbound_id is not None
            _required_identity(outbound_id, "delivery outbound_id", max_chars=256)
            where = "session_id = ? AND outbound_id = ?"
            params = (session_id, outbound_id, limit)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT outbound_id, state, generation, envelope_json, "
                "provider_message_id, error_code, created_at, updated_at FROM outbox "
                f"WHERE {where} ORDER BY created_at ASC, outbound_id ASC LIMIT ?",
                params,
            ).fetchall()
        return tuple(_outbound_record(str(row[0]), row[1:]) for row in rows)

    def append_event(
        self,
        *,
        generation: int,
        event: str,
        payload: Mapping[str, Any],
        now: float | None = None,
    ) -> GatewayEventRecord:
        _required_identity(event, "event")
        if event not in GATEWAY_EVENTS:
            raise ValueError("event is not part of the Gateway v1 surface")
        created_at = _timestamp(now)
        payload_json = _json_dump(payload)
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cursor = connection.execute(
                "INSERT INTO gateway_events(event, payload_json, generation, created_at) "
                "VALUES(?, ?, ?, ?)",
                (event, payload_json, generation, created_at),
            )
            if cursor.lastrowid is None:
                raise GatewayStateError("Gateway event sequence was not allocated")
            seq = int(cursor.lastrowid)
        return GatewayEventRecord(
            seq=seq,
            event=event,
            payload=_json_object(payload_json),
            created_at=created_at,
        )

    def events_after(self, seq: int, *, limit: int = 100) -> tuple[GatewayEventRecord, ...]:
        if seq < 0:
            raise ValueError("seq cannot be negative")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT seq, event, payload_json, created_at FROM gateway_events "
                "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                (seq, limit),
            ).fetchall()
        return tuple(
            GatewayEventRecord(
                seq=int(row[0]),
                event=str(row[1]),
                payload=_json_object(str(row[2])),
                created_at=float(row[3]),
            )
            for row in rows
        )

    def replay_events(self, seq: int, *, limit: int = 100) -> GatewayEventReplay:
        if seq < 0:
            raise ValueError("seq cannot be negative")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._read_connection() as connection:
            bounds = connection.execute("SELECT MIN(seq), MAX(seq) FROM gateway_events").fetchone()
            first = int(bounds[0]) if bounds is not None and bounds[0] is not None else None
            current = int(bounds[1]) if bounds is not None and bounds[1] is not None else 0
            resync_required = seq > current or (first is not None and seq < first - 1)
            rows = []
            if not resync_required:
                rows = connection.execute(
                    "SELECT seq, event, payload_json, created_at FROM gateway_events "
                    "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    (seq, limit),
                ).fetchall()
        events = tuple(
            GatewayEventRecord(
                seq=int(row[0]),
                event=str(row[1]),
                payload=_json_object(str(row[2])),
                created_at=float(row[3]),
            )
            for row in rows
        )
        return GatewayEventReplay(
            events=events,
            current_cursor=current,
            resync_required=resync_required,
        )

    def prune_events(self, *, generation: int, retain_last: int = 10_000) -> int:
        if retain_last < 1:
            raise ValueError("retain_last must be positive")
        with self._write_connection() as connection:
            self._assert_generation(connection, generation)
            cutoff = connection.execute(
                "SELECT seq FROM gateway_events ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (retain_last - 1,),
            ).fetchone()
            if cutoff is None:
                return 0
            cursor = connection.execute(
                "DELETE FROM gateway_events WHERE seq < ?",
                (int(cutoff[0]),),
            )
            return cursor.rowcount

    def delivery_receipts(self, outbound_id: str) -> tuple[DeliveryReceipt, ...]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT receipt_id, stage, observed_at, provider_message_id, "
                "error_code, detail_json FROM delivery_receipts "
                "WHERE outbound_id = ? ORDER BY rowid ASC",
                (outbound_id,),
            ).fetchall()
        return tuple(
            DeliveryReceipt(
                receipt_id=str(row[0]),
                outbound_id=outbound_id,
                stage=_delivery_stage(str(row[1])),
                observed_at=float(row[2]),
                provider_message_id=str(row[3]) if row[3] is not None else None,
                error_code=str(row[4]) if row[4] is not None else None,
                detail=_json_object(str(row[5])),
            )
            for row in rows
        )

    def _initialize_schema(self) -> None:
        with self._write_connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS gateway_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM gateway_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and int(row[0]) != SCHEMA_VERSION:
                raise GatewayStateError("Gateway database schema version is unsupported")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS idempotency(
                    client_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'pending', 'completed', 'recovery_required'
                    )),
                    response_json TEXT,
                    generation INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(client_id, method, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS sessions(
                    session_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    conversation_kind TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    writer_generation INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    debug INTEGER NOT NULL CHECK(debug IN (0, 1)),
                    event_cursor INTEGER NOT NULL CHECK(event_cursor >= 0),
                    active_run_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(channel, account_id, conversation_kind, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    input_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'accepted', 'running', 'abort_requested', 'recovery_required',
                        'completed', 'aborted', 'failed'
                    )),
                    generation INTEGER NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    recovery_from_state TEXT CHECK(recovery_from_state IS NULL OR
                        recovery_from_state IN ('accepted', 'running', 'abort_requested')
                    ),
                    created_at REAL NOT NULL,
                    started_at REAL,
                    abort_requested_at REAL,
                    finished_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_session
                ON runs(session_id)
                WHERE state IN (
                    'accepted', 'running', 'abort_requested', 'recovery_required'
                );
                CREATE TABLE IF NOT EXISTS approvals(
                    approval_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    run_id TEXT REFERENCES runs(run_id),
                    operation TEXT NOT NULL,
                    target TEXT NOT NULL,
                    params_digest TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    conversation_ref TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    challenge_digest TEXT NOT NULL,
                    challenge TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'resolved', 'expired')),
                    decision_id TEXT,
                    accepted INTEGER CHECK(accepted IS NULL OR accepted IN (0, 1)),
                    decided_at REAL,
                    generation INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK(
                        (state = 'pending' AND decision_id IS NULL AND accepted IS NULL
                            AND decided_at IS NULL AND challenge != '')
                        OR (state = 'resolved' AND decision_id IS NOT NULL
                            AND accepted IS NOT NULL AND decided_at IS NOT NULL
                            AND challenge = '')
                        OR (state = 'expired' AND decision_id IS NULL AND accepted IS NULL
                            AND decided_at IS NULL AND challenge = '')
                    )
                );
                CREATE INDEX IF NOT EXISTS approvals_actor_conversation
                ON approvals(actor_ref, conversation_ref, session_id, created_at, approval_id);
                CREATE TABLE IF NOT EXISTS authorization_decisions(
                    decision_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    allowed INTEGER NOT NULL CHECK(allowed IN (0, 1)),
                    code TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    observed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS authorization_decisions_actor
                ON authorization_decisions(actor_ref, observed_at, decision_id);
                CREATE TABLE IF NOT EXISTS ingress(
                    channel TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    frame_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    principal_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'accepted', 'processing', 'completed', 'failed', 'recovery_required'
                    )),
                    generation INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(channel, account_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS outbox(
                    outbound_id TEXT PRIMARY KEY,
                    envelope_sha256 TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    session_id TEXT,
                    run_id TEXT,
                    state TEXT NOT NULL CHECK(state IN (
                        'pending', 'submitting', 'provider_submitted',
                        'provider_acknowledged', 'delivery_unknown', 'failed'
                    )),
                    generation INTEGER NOT NULL,
                    provider_message_id TEXT,
                    error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK(run_id IS NULL OR session_id IS NOT NULL)
                );
                CREATE INDEX IF NOT EXISTS outbox_session_run
                    ON outbox(session_id, run_id, created_at, outbound_id);
                CREATE TABLE IF NOT EXISTS delivery_receipts(
                    receipt_id TEXT PRIMARY KEY,
                    outbound_id TEXT NOT NULL REFERENCES outbox(outbound_id),
                    stage TEXT NOT NULL CHECK(stage IN (
                        'gateway_accepted', 'provider_submitted', 'provider_acknowledged',
                        'delivery_unknown', 'platform_displayed', 'user_read', 'failed'
                    )),
                    observed_at REAL NOT NULL,
                    provider_message_id TEXT,
                    error_code TEXT,
                    detail_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gateway_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            if row is None:
                connection.execute(
                    "INSERT INTO gateway_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT INTO gateway_meta(key, value) VALUES('writer_generation', '0')"
                )

    def _require_session(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> SessionRecord:
        row = connection.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionConflict("Gateway session does not exist")
        return _session_record(session_id, row)

    def _require_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> RunRecord:
        row = connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunConflict("Gateway run does not exist")
        return _run_record(run_id, row)

    def _clear_active_run(
        self,
        connection: sqlite3.Connection,
        *,
        generation: int,
        session_id: str,
        run_id: str,
        observed_at: float,
    ) -> None:
        cursor = connection.execute(
            "UPDATE sessions SET active_run_id = NULL, writer_generation = ?, "
            "updated_at = ? WHERE session_id = ? AND active_run_id = ?",
            (generation, observed_at, session_id, run_id),
        )
        if cursor.rowcount != 1:
            raise RunConflict("Gateway session is no longer bound to this active run")

    def _assert_generation(self, connection: sqlite3.Connection, generation: int) -> None:
        if generation < 1:
            raise StaleWriterGeneration("Gateway writer generation must be positive")
        row = connection.execute(
            "SELECT value FROM gateway_meta WHERE key = 'writer_generation'"
        ).fetchone()
        current = int(row[0]) if row is not None else 0
        if generation != current:
            raise StaleWriterGeneration(
                f"Gateway writer generation {generation} is stale; current generation is {current}"
            )

    def _insert_delivery_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        receipt_id: str,
        outbound_id: str,
        stage: str,
        observed_at: float,
        provider_message_id: str | None,
        error_code: str | None,
        detail: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO delivery_receipts("
            "receipt_id, outbound_id, stage, observed_at, provider_message_id, "
            "error_code, detail_json"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                outbound_id,
                stage,
                observed_at,
                provider_message_id,
                error_code,
                _json_dump(detail),
            ),
        )

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            yield connection

    @contextmanager
    def _write_connection(
        self,
    ) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        _validate_private_root(self.root, trusted_anchor=self.trusted_anchor)
        _validate_private_database_file(self.database_path)
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            connection.close()
            _validate_sqlite_files(self.database_path)


def _ensure_private_root(path: Path, *, trusted_anchor: Path) -> None:
    relative = _relative_state_path(path, trusted_anchor)
    _validate_private_directory(trusted_anchor, label="Gateway trusted state anchor")
    current = trusted_anchor
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = current.lstat()
        _validate_private_directory_metadata(
            metadata,
            current,
            label="Gateway private state directory",
        )
    _validate_private_root(path, trusted_anchor=trusted_anchor)


def _validate_private_root(path: Path, *, trusted_anchor: Path) -> None:
    relative = _relative_state_path(path, trusted_anchor)
    _validate_private_directory(trusted_anchor, label="Gateway trusted state anchor")
    current = trusted_anchor
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise GatewayStateError("Gateway state root does not exist") from exc
        _validate_private_directory_metadata(
            metadata,
            current,
            label="Gateway private state directory",
        )


def _relative_state_path(path: Path, trusted_anchor: Path) -> Path:
    _reject_symlink_components(trusted_anchor)
    _reject_symlink_components(path)
    try:
        return path.relative_to(trusted_anchor)
    except ValueError as exc:
        raise GatewayStateError("Gateway state root must be inside its trusted anchor") from exc


def _validate_private_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise GatewayStateError(f"{label} does not exist") from exc
    _validate_private_directory_metadata(metadata, path, label=label)


def _validate_private_directory_metadata(
    metadata: os.stat_result,
    path: Path,
    *,
    label: str,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise GatewayStateError(f"{label} must be a directory: {path.name}")
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"{label} must be owned by the service user: {path.name}")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PermissionError(f"{label} must use mode 0700: {path.name}")


def _ensure_private_database_file(path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _validate_private_database_file(path)
        return
    except OSError as exc:
        raise GatewayStateError("Gateway database cannot be created safely") from exc
    try:
        _validate_private_file_metadata(os.fstat(descriptor), path)
    finally:
        os.close(descriptor)


def _validate_private_database_file(path: Path) -> None:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GatewayStateError("Gateway database cannot be opened safely") from exc
    try:
        _validate_private_file_metadata(os.fstat(descriptor), path)
    finally:
        os.close(descriptor)


def _acquire_instance_lease(path: Path) -> GatewayInstanceLease:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise GatewayStateError("Gateway instance leases require Linux or WSL")
    import fcntl

    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise GatewayStateError("Gateway instance lease cannot be opened safely") from exc
    try:
        _validate_open_private_file(path, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GatewayInstanceLeaseUnavailable(
                "Another Gateway process already owns this instance"
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise GatewayInstanceLeaseUnavailable(
                    "Another Gateway process already owns this instance"
                ) from exc
            raise GatewayStateError("Gateway instance lease cannot be acquired") from exc
        _validate_open_private_file(path, descriptor)
        return GatewayInstanceLease(descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _validate_open_private_file(path: Path, descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise GatewayStateError("Gateway state file cannot be inspected safely") from exc
    _validate_private_file_metadata(opened, path)
    _validate_private_file_metadata(current, path)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise GatewayStateError("Gateway state file changed during secure open")


def _validate_sqlite_files(database_path: Path) -> None:
    _validate_private_database_file(database_path)
    for suffix in ("-wal", "-shm", "-journal"):
        path = Path(str(database_path) + suffix)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        _validate_private_file_metadata(metadata, path)


def _validate_private_file_metadata(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise GatewayStateError(f"Gateway state file is not regular: {path.name}")
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"Gateway state file has the wrong owner: {path.name}")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError(f"Gateway state file must use mode 0600: {path.name}")
        if metadata.st_nlink != 1:
            raise GatewayStateError(
                f"Gateway state file must have exactly one hard link: {path.name}"
            )


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise GatewayStateError("Gateway state path cannot contain a symlink")


def _json_dump(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise GatewayStateError("Gateway state value is not valid JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_STATE_JSON_BYTES:
        raise GatewayStateError("Gateway state JSON exceeds the byte limit")
    return encoded


def _json_object(encoded: str) -> Mapping[str, Any]:
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise GatewayStateError("Gateway state JSON is corrupt") from exc
    if not isinstance(value, dict):
        raise GatewayStateError("Gateway state JSON must contain an object")
    return {str(key): item for key, item in value.items()}


def _required_identity(value: str, label: str, *, max_chars: int = 128) -> None:
    if not isinstance(value, str) or not value or len(value) > max_chars or "\x00" in value:
        raise ValueError(f"{label} is invalid")


def _require_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _timestamp(value: float | None) -> float:
    result = time.time() if value is None else value
    if type(result) not in {int, float} or not math.isfinite(result) or result < 0:
        raise ValueError("timestamp is invalid")
    return float(result)


def _validate_session_fields(
    *,
    session_id: str,
    account: ChannelAccountRef,
    conversation: ConversationRef,
    mode: str,
    debug: bool,
    event_cursor: int,
) -> None:
    _required_identity(session_id, "session_id", max_chars=256)
    _validate_account_conversation(account, conversation)
    _validate_session_mode(mode)
    if type(debug) is not bool:
        raise ValueError("session debug must be a boolean")
    if type(event_cursor) is not int or event_cursor < 0:
        raise ValueError("session event_cursor must be a non-negative integer")


def _validate_account_conversation(
    account: ChannelAccountRef,
    conversation: ConversationRef,
) -> None:
    if not isinstance(account, ChannelAccountRef):
        raise ValueError("account must be a ChannelAccountRef")
    if not isinstance(conversation, ConversationRef):
        raise ValueError("conversation must be a ConversationRef")
    _required_identity(account.channel, "channel", max_chars=64)
    _required_identity(account.account_id, "account_id", max_chars=256)
    _required_identity(conversation.kind, "conversation kind", max_chars=64)
    _required_identity(
        conversation.conversation_id,
        "conversation_id",
        max_chars=256,
    )


def _validate_session_mode(mode: str) -> None:
    if not isinstance(mode, str) or _SESSION_MODE_RE.fullmatch(mode) is None:
        raise ValueError("session mode is invalid")


def _validate_page(*, offset: int, limit: int) -> None:
    if type(offset) is not int or offset < 0:
        raise ValueError("session list offset must be a non-negative integer")
    if type(limit) is not int or limit < 1 or limit > 1000:
        raise ValueError("session list limit must be between 1 and 1000")


def _session_record(
    session_id: str,
    row: sqlite3.Row | tuple[Any, ...],
) -> SessionRecord:
    try:
        writer_generation = int(row[4])
        debug_value = int(row[6])
        event_cursor = int(row[7])
    except (TypeError, ValueError) as exc:
        raise GatewayStateError("Gateway session state is corrupt") from exc
    if writer_generation < 1 or debug_value not in {0, 1} or event_cursor < 0:
        raise GatewayStateError("Gateway session state is corrupt")
    account = ChannelAccountRef(channel=str(row[0]), account_id=str(row[1]))
    conversation = ConversationRef(kind=str(row[2]), conversation_id=str(row[3]))
    mode = str(row[5])
    try:
        _validate_session_fields(
            session_id=session_id,
            account=account,
            conversation=conversation,
            mode=mode,
            debug=bool(debug_value),
            event_cursor=event_cursor,
        )
    except ValueError as exc:
        raise GatewayStateError("Gateway session state is corrupt") from exc
    active_run_id = str(row[8]) if row[8] is not None else None
    if active_run_id is not None:
        try:
            _required_identity(active_run_id, "active_run_id", max_chars=256)
        except ValueError as exc:
            raise GatewayStateError("Gateway session state is corrupt") from exc
    return SessionRecord(
        session_id=session_id,
        account=account,
        conversation=conversation,
        writer_generation=writer_generation,
        mode=mode,
        debug=bool(debug_value),
        event_cursor=event_cursor,
        active_run_id=active_run_id,
        created_at=_stored_timestamp(row[9], "session created_at"),
        updated_at=_stored_timestamp(row[10], "session updated_at"),
    )


def _run_record(
    run_id: str,
    row: sqlite3.Row | tuple[Any, ...],
) -> RunRecord:
    state_value = str(row[2])
    if state_value not in RUN_STATES:
        raise GatewayStateError("Gateway run state is corrupt")
    try:
        generation = int(row[3])
    except (TypeError, ValueError) as exc:
        raise GatewayStateError("Gateway run generation is corrupt") from exc
    if generation < 1:
        raise GatewayStateError("Gateway run generation is corrupt")
    session_id = str(row[0])
    fingerprint = str(row[1])
    try:
        _required_identity(run_id, "run_id", max_chars=256)
        _required_identity(session_id, "session_id", max_chars=256)
        _require_sha256(fingerprint, "input_fingerprint")
    except ValueError as exc:
        raise GatewayStateError("Gateway run identity is corrupt") from exc
    result = _json_object(str(row[4])) if row[4] is not None else None
    error_code = str(row[5]) if row[5] is not None else None
    recovery_from_state = str(row[6]) if row[6] is not None else None
    if recovery_from_state is not None and recovery_from_state not in {
        "accepted",
        "running",
        "abort_requested",
    }:
        raise GatewayStateError("Gateway run recovery state is corrupt")
    if state_value == "recovery_required" and recovery_from_state is None:
        raise GatewayStateError("Gateway run recovery state is corrupt")
    if state_value in TERMINAL_RUN_STATES and row[10] is None:
        raise GatewayStateError("Gateway terminal run state is corrupt")
    if state_value in ACTIVE_RUN_STATES and (row[4] is not None or row[10] is not None):
        raise GatewayStateError("Gateway active run state is corrupt")
    if state_value == "failed":
        if error_code is None:
            raise GatewayStateError("Gateway failed run state is corrupt")
    elif error_code is not None:
        raise GatewayStateError("Gateway run error state is corrupt")
    return RunRecord(
        run_id=run_id,
        session_id=session_id,
        input_fingerprint=fingerprint,
        state=cast(RunState, state_value),
        generation=generation,
        result=result,
        error_code=error_code,
        recovery_from_state=recovery_from_state,
        created_at=_stored_timestamp(row[7], "run created_at"),
        started_at=_optional_stored_timestamp(row[8], "run started_at"),
        abort_requested_at=_optional_stored_timestamp(
            row[9],
            "run abort_requested_at",
        ),
        finished_at=_optional_stored_timestamp(row[10], "run finished_at"),
        updated_at=_stored_timestamp(row[11], "run updated_at"),
    )


def _stored_timestamp(value: Any, label: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise GatewayStateError(f"Gateway {label} is corrupt")
    return float(value)


def _optional_stored_timestamp(value: Any, label: str) -> float | None:
    return None if value is None else _stored_timestamp(value, label)


def _assert_run_session(record: RunRecord, session_id: str) -> None:
    if record.session_id != session_id:
        raise RunConflict("Gateway run belongs to a different session")


def _assert_session_run_binding(session: SessionRecord, run_id: str) -> None:
    if session.active_run_id != run_id:
        raise RunConflict("Gateway session is not bound to this active run")


def _replace_run_state(record: RunRecord, **changes: Any) -> RunRecord:
    return replace(record, **changes)


def _validate_run_terminal_payload(
    *,
    outcome: RunOutcome,
    result: Mapping[str, Any] | None,
    error_code: str | None,
    worker_stop_reason: Literal["cancelled"] | None,
) -> str | None:
    if outcome not in TERMINAL_RUN_STATES:
        raise ValueError("run outcome is invalid")
    if result is not None and not isinstance(result, Mapping):
        raise ValueError("run result must be a JSON object")
    normalized_result = dict(result) if result is not None else None
    if normalized_result is not None and any(not isinstance(key, str) for key in normalized_result):
        raise ValueError("run result keys must be strings")
    result_json = _json_dump(normalized_result) if normalized_result is not None else None
    if outcome == "failed":
        if error_code is None:
            raise ValueError("failed run requires an error_code")
        _required_identity(error_code, "error_code", max_chars=128)
    elif error_code is not None:
        raise ValueError("only a failed run can include an error_code")
    if outcome == "aborted":
        if worker_stop_reason != "cancelled":
            raise RunConflict("aborted requires a worker cancelled result")
    elif worker_stop_reason is not None:
        raise ValueError("worker_stop_reason is valid only for an aborted run")
    return result_json


def _validate_approval_request(request: ApprovalRequest) -> None:
    if not isinstance(request, ApprovalRequest):
        raise ValueError("request must be an ApprovalRequest")
    _required_identity(request.approval_id, "approval_id", max_chars=256)
    _required_identity(request.session_id, "approval session_id", max_chars=256)
    if request.run_id is not None:
        _required_identity(request.run_id, "approval run_id", max_chars=256)
    _required_identity(request.operation, "approval operation", max_chars=256)
    _required_identity(request.target, "approval target", max_chars=512)
    _require_prefixed_sha256(request.params_digest, "approval params_digest")
    _required_identity(request.actor_ref, "approval actor_ref", max_chars=512)
    _required_identity(
        request.conversation_ref,
        "approval conversation_ref",
        max_chars=512,
    )
    _required_identity(
        request.policy_version,
        "approval policy_version",
        max_chars=256,
    )
    _require_prefixed_sha256(
        request.challenge_digest,
        "approval challenge_digest",
    )
    if (
        type(request.expires_at) not in {int, float}
        or not math.isfinite(request.expires_at)
        or request.expires_at <= 0
    ):
        raise ValueError("approval expires_at is invalid")


def _validate_authorization_decision(decision: AuthorizationDecision) -> None:
    if not isinstance(decision, AuthorizationDecision):
        raise ValueError("decision must be an AuthorizationDecision")
    _required_identity(decision.decision_id, "authorization decision_id", max_chars=256)
    _required_identity(decision.request_id, "authorization request_id", max_chars=256)
    _require_prefixed_sha256(
        decision.request_digest,
        "authorization request_digest",
    )
    if type(decision.allowed) is not bool:
        raise ValueError("authorization allowed must be a boolean")
    _required_identity(decision.code, "authorization code", max_chars=256)
    _required_identity(
        decision.policy_version,
        "authorization policy_version",
        max_chars=256,
    )
    _required_identity(decision.actor_ref, "authorization actor_ref", max_chars=512)


def _authorization_decision_record(
    decision_id: str,
    row: sqlite3.Row | tuple[Any, ...],
) -> AuthorizationDecisionRecord:
    try:
        generation = int(row[6])
        observed_at = _stored_timestamp(row[7], "authorization observed_at")
    except (TypeError, ValueError) as exc:
        raise GatewayStateError("Gateway authorization decision is corrupt") from exc
    if generation < 1 or type(row[2]) is not int or int(row[2]) not in {0, 1}:
        raise GatewayStateError("Gateway authorization decision is corrupt")
    decision = AuthorizationDecision(
        decision_id=decision_id,
        request_id=str(row[0]),
        request_digest=str(row[1]),
        allowed=bool(row[2]),
        code=str(row[3]),
        policy_version=str(row[4]),
        actor_ref=str(row[5]),
    )
    try:
        _validate_authorization_decision(decision)
    except ValueError as exc:
        raise GatewayStateError("Gateway authorization decision is corrupt") from exc
    return AuthorizationDecisionRecord(
        decision=decision,
        generation=generation,
        observed_at=observed_at,
    )


def _validate_approval_resolution(resolution: ApprovalResolution) -> None:
    if not isinstance(resolution, ApprovalResolution):
        raise ValueError("resolution must be an ApprovalResolution")
    _required_identity(resolution.approval_id, "approval_id", max_chars=256)
    _required_identity(resolution.actor_ref, "approval actor_ref", max_chars=512)
    _required_identity(
        resolution.conversation_ref,
        "approval conversation_ref",
        max_chars=512,
    )
    _require_prefixed_sha256(
        resolution.params_digest,
        "approval params_digest",
    )
    _required_identity(
        resolution.policy_version,
        "approval policy_version",
        max_chars=256,
    )
    _validate_approval_challenge(resolution.challenge)
    if type(resolution.accepted) is not bool:
        raise ValueError("approval decision must be a boolean")


def _validate_approval_access(
    *,
    actor_ref: str,
    conversation_ref: str,
    session_id: str | None,
) -> None:
    _required_identity(actor_ref, "approval actor_ref", max_chars=512)
    _required_identity(
        conversation_ref,
        "approval conversation_ref",
        max_chars=512,
    )
    if session_id is not None:
        _required_identity(session_id, "approval session_id", max_chars=256)


def _validate_approval_challenge(challenge: str) -> None:
    if not isinstance(challenge, str) or _APPROVAL_CHALLENGE_RE.fullmatch(challenge) is None:
        raise ValueError("approval challenge must be an opaque URL-safe value")


def _challenge_digest(challenge: str) -> str:
    return "sha256:" + hashlib.sha256(challenge.encode("utf-8")).hexdigest()


def _require_prefixed_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{label} must be a prefixed lowercase SHA-256 digest")
    digest = value[7:]
    try:
        _require_sha256(digest, label)
    except ValueError as exc:
        raise ValueError(f"{label} must be a prefixed lowercase SHA-256 digest") from exc


def _approval_record(
    approval_id: str,
    row: sqlite3.Row | tuple[Any, ...],
    *,
    now: float,
) -> ApprovalRecord:
    try:
        expires_at = _stored_timestamp(row[10], "approval expires_at")
        generation = int(row[15])
        created_at = _stored_timestamp(row[16], "approval created_at")
        updated_at = _stored_timestamp(row[17], "approval updated_at")
    except (TypeError, ValueError) as exc:
        raise GatewayStateError("Gateway approval state is corrupt") from exc
    if generation < 1 or expires_at <= created_at or updated_at < created_at:
        raise GatewayStateError("Gateway approval state is corrupt")
    request = ApprovalRequest(
        approval_id=approval_id,
        session_id=str(row[0]),
        run_id=str(row[1]) if row[1] is not None else None,
        operation=str(row[2]),
        target=str(row[3]),
        params_digest=str(row[4]),
        actor_ref=str(row[5]),
        conversation_ref=str(row[6]),
        policy_version=str(row[7]),
        challenge_digest=str(row[8]),
        expires_at=expires_at,
    )
    try:
        _validate_approval_request(request)
    except ValueError as exc:
        raise GatewayStateError("Gateway approval binding is corrupt") from exc
    stored_state = str(row[11])
    if stored_state not in {"pending", "resolved", "expired"}:
        raise GatewayStateError("Gateway approval state is corrupt")
    challenge = str(row[9])
    decision_id = str(row[12]) if row[12] is not None else None
    accepted_raw = row[13]
    accepted = bool(accepted_raw) if accepted_raw is not None else None
    decided_at = _optional_stored_timestamp(row[14], "approval decided_at")
    if stored_state == "pending":
        try:
            _validate_approval_challenge(challenge)
        except ValueError as exc:
            raise GatewayStateError("Gateway pending approval challenge is corrupt") from exc
        if (
            not hmac.compare_digest(_challenge_digest(challenge), request.challenge_digest)
            or decision_id is not None
            or accepted is not None
            or decided_at is not None
        ):
            raise GatewayStateError("Gateway pending approval state is corrupt")
        status = "expired" if now >= expires_at else "pending"
        visible_challenge = challenge if status == "pending" else None
    elif stored_state == "resolved":
        if (
            challenge
            or decision_id is None
            or accepted_raw not in {0, 1}
            or decided_at is None
            or decided_at >= expires_at
        ):
            raise GatewayStateError("Gateway resolved approval state is corrupt")
        status = "resolved"
        visible_challenge = None
    else:
        if challenge or decision_id is not None or accepted is not None or decided_at is not None:
            raise GatewayStateError("Gateway expired approval state is corrupt")
        status = "expired"
        visible_challenge = None
    return ApprovalRecord(
        request=request,
        status=cast(Literal["pending", "resolved", "expired"], status),
        challenge=visible_challenge,
        decision_id=decision_id,
        accepted=accepted,
        decided_at=decided_at,
        generation=generation,
        created_at=created_at,
        updated_at=updated_at,
    )


def _assert_terminal_run_replay(
    record: RunRecord,
    *,
    outcome: RunOutcome,
    result: Mapping[str, Any] | None,
    error_code: str | None,
) -> None:
    normalized_result = dict(result) if result is not None else None
    if (
        record.state != outcome
        or record.result != normalized_result
        or record.error_code != error_code
    ):
        raise RunConflict("terminal Gateway run cannot be rewritten")


def _automatic_receipt_id(
    outbound_id: str,
    stage: str,
    generation: int,
    observed_at: float,
) -> str:
    digest = hashlib.sha256(
        f"{outbound_id}\0{stage}\0{generation}\0{observed_at!r}".encode("utf-8")
    ).hexdigest()[:24]
    return f"receipt_{digest}"


def _canonical_inbound_event(payload: Mapping[str, Any]) -> CanonicalInboundEvent:
    """Rehydrate only the exact canonical shape written by ``reserve_ingress``."""

    try:
        evidence_payload = _state_mapping(payload["evidence"])
        account_payload = _state_mapping(evidence_payload["account"])
        conversation_payload = _state_mapping(evidence_payload["conversation"])
        sender_payload = _state_mapping(evidence_payload["sender"])
        account = ChannelAccountRef(
            channel=str(account_payload["channel"]),
            account_id=str(account_payload["account_id"]),
        )
        conversation = ConversationRef(
            kind=str(conversation_payload["kind"]),
            conversation_id=str(conversation_payload["conversation_id"]),
        )
        sender = SenderClaim(
            sender_id=str(sender_payload["sender_id"]),
            display_name=(
                str(sender_payload["display_name"])
                if sender_payload["display_name"] is not None
                else None
            ),
        )
        evidence = TransportEvidence(
            account=account,
            conversation=conversation,
            sender=sender,
            event_id=str(evidence_payload["event_id"]),
            message_id=(
                str(evidence_payload["message_id"])
                if evidence_payload["message_id"] is not None
                else None
            ),
            connection_generation=str(evidence_payload["connection_generation"]),
            frame_sha256=str(evidence_payload["frame_sha256"]),
            observed_at=float(evidence_payload["observed_at"]),
        )
        segments = tuple(
            MessageSegment(
                kind=cast(Any, item["kind"]),
                text=str(item["text"]) if item["text"] is not None else None,
                target=str(item["target"]) if item["target"] is not None else None,
                resource_ticket_id=(
                    str(item["resource_ticket_id"])
                    if item["resource_ticket_id"] is not None
                    else None
                ),
                data=dict(_state_mapping(item["data"])),
            )
            for item in (_state_mapping(value) for value in _state_array(payload["segments"]))
        )
        tickets = tuple(
            ResourceTicket(
                ticket_id=str(item["ticket_id"]),
                account=ChannelAccountRef(**dict(_state_mapping(item["account"]))),
                conversation=ConversationRef(
                    **dict(_state_mapping(item["conversation"]))
                ),
                sender_id=str(item["sender_id"]),
                event_id=str(item["event_id"]),
                message_id=(
                    str(item["message_id"]) if item["message_id"] is not None else None
                ),
                kind=cast(Any, item["kind"]),
                name=str(item["name"]) if item["name"] is not None else None,
                media_type=(
                    str(item["media_type"]) if item["media_type"] is not None else None
                ),
                size_bytes=(
                    int(item["size_bytes"]) if item["size_bytes"] is not None else None
                ),
                sha256=str(item["sha256"]) if item["sha256"] is not None else None,
                expires_at=(
                    float(item["expires_at"])
                    if item["expires_at"] is not None
                    else None
                ),
                provider_ref=dict(_state_mapping(item["provider_ref"])),
            )
            for item in (
                _state_mapping(value) for value in _state_array(payload["resource_tickets"])
            )
        )
        event = CanonicalInboundEvent(
            evidence=evidence,
            segments=segments,
            resource_tickets=tickets,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayStateError("Gateway ingress payload is corrupt") from exc
    if _json_dump(asdict(event)) != _json_dump(payload):
        raise GatewayStateError("Gateway ingress payload shape is corrupt")
    return event


def _stored_principal(encoded: str) -> Principal:
    payload = _json_object(encoded)
    try:
        conversation_payload = _state_mapping(payload["conversation"])
        principal = Principal(
            channel=str(payload["channel"]),
            account_id=str(payload["account_id"]),
            conversation=ConversationIdentity(
                platform=str(conversation_payload["platform"]),
                chat_kind=str(conversation_payload["chat_kind"]),
                chat_id=str(conversation_payload["chat_id"]),
            ),
            user_id=str(payload["user_id"]),
            role=Role(str(payload["role"])),
            evidence_digest=str(payload["evidence_digest"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayStateError("Gateway ingress principal is corrupt") from exc
    if _json_dump(asdict(principal)) != _json_dump(payload):
        raise GatewayStateError("Gateway ingress principal shape is corrupt")
    return principal


def _validate_ingress_principal(
    principal: Principal,
    event: CanonicalInboundEvent,
) -> None:
    if not isinstance(principal, Principal):
        raise ValueError("ingress principal must be a Principal")
    evidence = event.evidence
    if (
        principal.channel != evidence.account.channel
        or principal.account_id != evidence.account.account_id
        or principal.conversation.platform != evidence.account.channel
        or principal.conversation.chat_kind != evidence.conversation.kind
        or principal.conversation.chat_id != evidence.conversation.conversation_id
        or principal.user_id != evidence.sender.sender_id
    ):
        raise ValueError("ingress principal does not match transport evidence")
    if not isinstance(principal.role, Role):
        raise ValueError("ingress principal role is invalid")
    _require_prefixed_sha256(principal.evidence_digest, "ingress evidence_digest")


def _state_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError("Gateway state value is not an object")
    return value


def _state_array(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise TypeError("Gateway state value is not an array")
    return tuple(value)


def _outbound_record(outbound_id: str, row: sqlite3.Row | tuple[Any, ...]) -> OutboundRecord:
    state = str(row[0])
    if state not in OUTBOX_STATES:
        raise GatewayStateError("Gateway outbox state is corrupt")
    return OutboundRecord(
        outbound_id=outbound_id,
        state=state,
        generation=int(row[1]),
        envelope=_json_object(str(row[2])),
        provider_message_id=str(row[3]) if row[3] is not None else None,
        error_code=str(row[4]) if row[4] is not None else None,
        created_at=float(row[5]),
        updated_at=float(row[6]),
    )


def _ingress_record(
    channel: str,
    account_id: str,
    event_id: str,
    row: sqlite3.Row | tuple[Any, ...],
) -> IngressRecord:
    payload = _json_object(str(row[3]))
    event = _canonical_inbound_event(payload)
    principal = _stored_principal(str(row[4]))
    try:
        _validate_ingress_principal(principal, event)
    except ValueError as exc:
        raise GatewayStateError("Gateway ingress principal is corrupt") from exc
    if (
        event.evidence.account.channel != channel
        or event.evidence.account.account_id != account_id
        or event.evidence.event_id != event_id
        or event.evidence.frame_sha256 != str(row[0])
    ):
        raise GatewayStateError("Gateway ingress identity is corrupt")
    return IngressRecord(
        channel=channel,
        account_id=account_id,
        event_id=event_id,
        frame_sha256=str(row[0]),
        state=_ingress_state(str(row[1])),
        generation=int(row[2]),
        payload=payload,
        event=event,
        principal=principal,
        created_at=_stored_timestamp(row[5], "ingress created_at"),
        updated_at=_stored_timestamp(row[6], "ingress updated_at"),
    )


def _ingress_state(value: str) -> str:
    if value not in INGRESS_STATES:
        raise GatewayStateError("Gateway ingress state is corrupt")
    return value


def _validate_ingress_retention(value: int) -> None:
    if type(value) is not int or value < 1 or value > 1_000_000:
        raise ValueError("ingress retention must be between 1 and 1000000")


def _prune_terminal_ingress_rows(
    connection: sqlite3.Connection,
    *,
    retain_last: int,
) -> int:
    cursor = connection.execute(
        "DELETE FROM ingress WHERE rowid IN ("
        "SELECT rowid FROM ingress WHERE state IN ('completed', 'failed') "
        "ORDER BY updated_at DESC, created_at DESC, channel DESC, account_id DESC, "
        "event_id DESC LIMIT -1 OFFSET ?"
        ")",
        (retain_last,),
    )
    return cursor.rowcount


def _delivery_stage(value: str) -> DeliveryStage:
    if value not in DELIVERY_STAGES:
        raise GatewayStateError("Gateway delivery receipt stage is corrupt")
    return cast(DeliveryStage, value)


__all__ = [
    "ApprovalConflict",
    "ApprovalRecord",
    "AuthorizationDecisionRecord",
    "GatewayEventRecord",
    "GatewayEventReplay",
    "GatewayInstanceLease",
    "GatewayInstanceLeaseUnavailable",
    "GatewayStateError",
    "GatewayStateStore",
    "IdempotencyConflict",
    "IdempotencyReservation",
    "IngressConflict",
    "IngressRecord",
    "IngressReservation",
    "OutboundConflict",
    "OutboundRecord",
    "RunConflict",
    "RunOutcome",
    "RunRecord",
    "RunState",
    "SCHEMA_VERSION",
    "SessionConflict",
    "SessionRecord",
    "StaleWriterGeneration",
]
