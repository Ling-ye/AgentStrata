"""Durable-first Gateway event publication and per-client visibility."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Protocol

from chatcopilot.contracts.gateway_protocol import EventFrame
from chatcopilot.contracts.gateway_rpc import (
    ApprovalRequestedEvent,
    ChatErrorEvent,
    ChatFinalEvent,
    ChatUpdateEvent,
    DeliveryUpdatedEvent,
    GatewayEventPayload,
    SessionUpdatedEvent,
)

from .application import GatewaySessionService, is_gateway_admin
from .rpc_validation import parse_event_payload, serialize_event_payload
from .server import GatewayClientContext
from .state_store import GatewayStateError, GatewayStateStore


class LiveEventSink(Protocol):
    def __call__(self, frame: EventFrame) -> Any: ...


class GatewayEventPublisher:
    """Append and cursor-bind every new event before attempting live delivery."""

    def __init__(
        self,
        *,
        state_store: GatewayStateStore,
        sessions: GatewaySessionService,
        generation: int,
        live_sink: LiveEventSink | None = None,
    ) -> None:
        if generation != sessions.generation:
            raise ValueError("event publisher generation does not match session service")
        self._state_store = state_store
        self._sessions = sessions
        self._generation = generation
        self._live_sink = live_sink
        self._live_loop: asyncio.AbstractEventLoop | None = None
        self._live_thread_id: int | None = None
        self._live_cursor = 0
        self._requested_live_cursor = 0
        self._drain_scheduled = False
        self._drain_lock = threading.Lock()
        self._live_failures = 0
        self._failure_lock = threading.Lock()

    @property
    def live_failures(self) -> int:
        with self._failure_lock:
            return self._live_failures

    def attach_live_sink(
        self,
        sink: LiveEventSink,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Bind the one live server after composition; durable emission works without it."""

        if self._live_sink is not None and self._live_sink is not sink:
            raise RuntimeError("Gateway live event sink is already attached")
        resolved_loop = loop
        if resolved_loop is None:
            try:
                resolved_loop = asyncio.get_running_loop()
            except RuntimeError:
                resolved_loop = None
        current_cursor = self._state_store.replay_events(0, limit=1).current_cursor
        with self._drain_lock:
            self._live_sink = sink
            self._live_loop = resolved_loop
            self._live_thread_id = (
                threading.get_ident() if resolved_loop is not None else None
            )
            self._live_cursor = current_cursor
            self._requested_live_cursor = current_cursor

    def emit(
        self,
        event: str,
        payload: GatewayEventPayload,
        *,
        session_id: str | None = None,
    ) -> EventFrame:
        """Persist a closed-schema event, update its session cursor, then publish live."""

        encoded = serialize_event_payload(event, payload)
        record = self._state_store.append_event(
            generation=self._generation,
            event=event,
            payload=encoded,
        )
        if session_id is not None:
            self._sessions.update_event_cursor(
                session_id=session_id,
                event_cursor=record.seq,
            )
        frame = EventFrame(event=record.event, seq=record.seq, payload=record.payload)
        self._request_live_drain(frame.seq)
        return frame

    def publish(self, frame: EventFrame) -> Any:
        """Publish an event already appended by another Gateway owner, such as outbox."""

        payload = parse_event_payload(frame.event, frame.payload)
        persisted = self._state_store.events_after(frame.seq - 1, limit=1)
        if (
            not persisted
            or persisted[0].seq != frame.seq
            or persisted[0].event != frame.event
            or dict(persisted[0].payload) != dict(frame.payload)
        ):
            raise GatewayStateError("live event does not match durable Gateway state")
        session_id = _event_session_id(payload)
        if session_id is not None:
            self._sessions.update_event_cursor(
                session_id=session_id,
                event_cursor=frame.seq,
            )
        self._request_live_drain(frame.seq)
        return None

    def _request_live_drain(self, target_seq: int) -> None:
        with self._drain_lock:
            sink = self._live_sink
            if sink is None:
                return
            self._requested_live_cursor = max(self._requested_live_cursor, target_seq)
            if self._drain_scheduled:
                return
            self._drain_scheduled = True
            loop = self._live_loop
        if loop is None:
            self._drain_live_events()
            return
        if loop.is_closed():
            with self._drain_lock:
                self._drain_scheduled = False
            self._record_live_failure()
            return
        loop.call_soon_threadsafe(self._drain_live_events)

    def _drain_live_events(self) -> None:
        while True:
            with self._drain_lock:
                sink = self._live_sink
                cursor = self._live_cursor
                target = self._requested_live_cursor
            if sink is None:
                with self._drain_lock:
                    self._drain_scheduled = False
                return
            if cursor < target:
                records = self._state_store.events_after(cursor, limit=min(256, target - cursor))
                if not records or records[0].seq != cursor + 1:
                    self._record_live_failure()
                    with self._drain_lock:
                        self._drain_scheduled = False
                    return
                for record in records:
                    if record.seq > target:
                        break
                    frame = EventFrame(record.event, record.seq, record.payload)
                    try:
                        sink(frame)
                    except Exception:
                        self._record_live_failure()
                    with self._drain_lock:
                        self._live_cursor = record.seq
                continue
            with self._drain_lock:
                if self._requested_live_cursor <= self._live_cursor:
                    self._drain_scheduled = False
                    return

    def _record_live_failure(self) -> None:
        with self._failure_lock:
            self._live_failures += 1


class GatewaySessionEventVisibility:
    """Expose session events only to their server-bound client owner or an administrator."""

    def __init__(self, sessions: GatewaySessionService) -> None:
        self._sessions = sessions

    def can_view(
        self,
        *,
        client: GatewayClientContext,
        event: EventFrame,
    ) -> bool:
        try:
            payload = parse_event_payload(event.event, event.payload)
            if is_gateway_admin(client):
                return True
            session_id = _event_session_id(payload)
            if session_id is None:
                return False
            record = self._sessions.state_store.get_session(session_id)
            return record is not None and self._sessions.can_access(
                client=client,
                session=record,
            )
        except Exception:
            return False


def _event_session_id(payload: GatewayEventPayload) -> str | None:
    if isinstance(payload, SessionUpdatedEvent):
        return payload.session.session_id
    if isinstance(payload, (ChatUpdateEvent, ChatFinalEvent, ChatErrorEvent)):
        return payload.session_id
    if isinstance(payload, ApprovalRequestedEvent):
        return payload.approval.session_id
    if isinstance(payload, DeliveryUpdatedEvent):
        return payload.session_id
    return None


__all__ = [
    "GatewayEventPublisher",
    "GatewaySessionEventVisibility",
    "LiveEventSink",
]
