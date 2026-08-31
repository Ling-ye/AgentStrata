"""In-memory ownership and concurrency rules for application sessions."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
import threading

from chatcopilot.application.sessions.model import (
    ActorExecutionState,
    ActorSessionKey,
    GatewaySessionState,
)
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.workspace import normalize_chat_kind


class SessionManagerError(RuntimeError):
    """Base error with a stable code suitable for a Gateway response."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SessionNotFoundError(SessionManagerError):
    def __init__(self, session_id: str) -> None:
        super().__init__(
            "session_not_found",
            f"Gateway session does not exist: {session_id}",
        )


class SessionConflictError(SessionManagerError):
    pass


class StaleSessionGenerationError(SessionManagerError):
    pass


class ActiveRunConflictError(SessionManagerError):
    pass


class ActorStateConflictError(SessionManagerError):
    pass


class ActorEvictionError(SessionManagerError):
    pass


class SessionManager:
    """Own immutable control state and actor-isolated execution handles.

    Durable sessions and runs remain Gateway storage responsibilities. This object
    supplies one-process atomicity, conversation lanes, and writer fencing for the
    application runtime assembled by the active Gateway generation.
    """

    def __init__(
        self,
        *,
        writer_generation: int,
        max_actors_per_session: int = 32,
    ) -> None:
        if type(writer_generation) is not int or writer_generation < 1:
            raise ValueError("writer_generation must be a positive integer")
        if type(max_actors_per_session) is not int or max_actors_per_session < 1:
            raise ValueError("max_actors_per_session must be a positive integer")
        self._writer_generation = writer_generation
        self._max_actors_per_session = max_actors_per_session
        self._sessions: dict[str, GatewaySessionState] = {}
        self._actors: dict[str, OrderedDict[str, ActorExecutionState]] = {}
        self._agent_session_owners: dict[int, ActorSessionKey] = {}
        self._conversation_lanes: dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self._lock = threading.RLock()

    @property
    def writer_generation(self) -> int:
        return self._writer_generation

    def create_session(
        self,
        *,
        session_id: str,
        account: ChannelAccountRef,
        conversation: ConversationRef,
        generation: int,
        mode: str = "default",
        debug: bool = False,
        event_cursor: int = 0,
        active_run_id: str | None = None,
    ) -> GatewaySessionState:
        self._assert_generation(generation)
        state = GatewaySessionState(
            session_id=session_id,
            account=account,
            conversation=conversation,
            writer_generation=generation,
            mode=mode,
            debug=debug,
            event_cursor=event_cursor,
            active_run_id=active_run_id,
        )
        with self._lock:
            if session_id in self._sessions:
                raise SessionConflictError(
                    "session_already_exists",
                    "Gateway session already exists",
                )
            self._sessions[session_id] = state
        return state

    def get_session(self, session_id: str) -> GatewaySessionState:
        with self._lock:
            return self._require_session(session_id)

    def list_sessions(self) -> tuple[GatewaySessionState, ...]:
        with self._lock:
            return tuple(self._sessions.values())

    def patch_session(
        self,
        session_id: str,
        *,
        generation: int,
        mode: str | None = None,
        debug: bool | None = None,
        event_cursor: int | None = None,
    ) -> GatewaySessionState:
        self._assert_generation(generation)
        with self._lock:
            current = self._require_session(session_id)
            if event_cursor is not None and event_cursor < current.event_cursor:
                raise SessionConflictError(
                    "event_cursor_regression",
                    "Gateway event cursor cannot move backwards",
                )
            updated = replace(
                current,
                mode=current.mode if mode is None else mode,
                debug=current.debug if debug is None else debug,
                event_cursor=(current.event_cursor if event_cursor is None else event_cursor),
            )
            self._sessions[session_id] = updated
            return updated

    def begin_run(
        self,
        session_id: str,
        run_id: str,
        *,
        generation: int,
    ) -> GatewaySessionState:
        self._assert_generation(generation)
        with self._lock:
            current = self._require_session(session_id)
            if current.active_run_id not in {None, run_id}:
                raise ActiveRunConflictError(
                    "session_run_active",
                    "Gateway session already has an active run",
                )
            if current.active_run_id == run_id:
                return current
            updated = replace(current, active_run_id=run_id)
            self._sessions[session_id] = updated
            return updated

    def finish_run(
        self,
        session_id: str,
        run_id: str,
        *,
        generation: int,
    ) -> GatewaySessionState:
        self._assert_generation(generation)
        with self._lock:
            current = self._require_session(session_id)
            if current.active_run_id != run_id:
                raise ActiveRunConflictError(
                    "active_run_mismatch",
                    "Only the active run can clear Gateway session state",
                )
            updated = replace(current, active_run_id=None)
            self._sessions[session_id] = updated
            return updated

    def conversation_lane(self, session_id: str) -> asyncio.Lock:
        """Return the shared ordering lock for the session's conversation."""

        with self._lock:
            state = self._require_session(session_id)
            key = _conversation_key(state)
            lane = self._conversation_lanes.get(key)
            if lane is None:
                lane = asyncio.Lock()
                self._conversation_lanes[key] = lane
            return lane

    def store_actor(
        self,
        state: ActorExecutionState,
        *,
        generation: int,
    ) -> ActorExecutionState | None:
        """Create or safely enrich one actor state, returning any LRU eviction."""

        self._assert_generation(generation)
        if state.writer_generation != generation:
            raise StaleSessionGenerationError(
                "actor_generation_stale",
                "Actor execution state belongs to another Gateway generation",
            )
        with self._lock:
            parent = self._require_session(state.key.gateway_session_id)
            self._assert_actor_conversation(parent, state)
            bucket = self._actors.setdefault(parent.session_id, OrderedDict())
            current = bucket.get(state.key.actor_ref)
            if current is not None:
                self._validate_actor_update(current, state)
                self._assert_agent_session_available(state)
                bucket[state.key.actor_ref] = state
                bucket.move_to_end(state.key.actor_ref)
                self._bind_agent_session(state)
                return None

            self._assert_agent_session_available(state)
            evicted: ActorExecutionState | None = None
            if len(bucket) >= self._max_actors_per_session:
                _, candidate = next(iter(bucket.items()))
                self._discard_execution_session(candidate)
                bucket.pop(candidate.key.actor_ref)
                self._unbind_agent_session(candidate)
                evicted = candidate
            bucket[state.key.actor_ref] = state
            self._bind_agent_session(state)
            return evicted

    def get_actor(
        self,
        key: ActorSessionKey,
    ) -> ActorExecutionState | None:
        with self._lock:
            self._require_session(key.gateway_session_id)
            bucket = self._actors.get(key.gateway_session_id)
            if bucket is None:
                return None
            return bucket.get(key.actor_ref)

    def touch_actor(
        self,
        key: ActorSessionKey,
        *,
        generation: int,
    ) -> ActorExecutionState | None:
        self._assert_generation(generation)
        with self._lock:
            self._require_session(key.gateway_session_id)
            bucket = self._actors.get(key.gateway_session_id)
            if bucket is None:
                return None
            state = bucket.get(key.actor_ref)
            if state is not None:
                bucket.move_to_end(key.actor_ref)
            return state

    def actor_keys(self, session_id: str) -> tuple[ActorSessionKey, ...]:
        with self._lock:
            self._require_session(session_id)
            bucket = self._actors.get(session_id, OrderedDict())
            return tuple(state.key for state in bucket.values())

    def evict_actor(
        self,
        key: ActorSessionKey,
        *,
        generation: int,
    ) -> ActorExecutionState | None:
        self._assert_generation(generation)
        with self._lock:
            self._require_session(key.gateway_session_id)
            bucket = self._actors.get(key.gateway_session_id)
            if bucket is None:
                return None
            state = bucket.get(key.actor_ref)
            if state is None:
                return None
            self._discard_execution_session(state)
            bucket.pop(key.actor_ref)
            self._unbind_agent_session(state)
            if not bucket:
                self._actors.pop(key.gateway_session_id, None)
            return state

    def evict_session_actors(
        self,
        session_id: str,
        *,
        generation: int,
    ) -> tuple[ActorExecutionState, ...]:
        """Discard every actor handle owned by one Gateway session."""

        self._assert_generation(generation)
        with self._lock:
            self._require_session(session_id)
            bucket = self._actors.get(session_id)
            if not bucket:
                return ()
            evicted: list[ActorExecutionState] = []
            for actor_ref in tuple(bucket):
                state = bucket[actor_ref]
                self._discard_execution_session(state)
                bucket.pop(actor_ref)
                self._unbind_agent_session(state)
                evicted.append(state)
            self._actors.pop(session_id, None)
            return tuple(evicted)

    def evict_all_actors(
        self,
        *,
        generation: int,
    ) -> tuple[ActorExecutionState, ...]:
        """Discard all process-local execution handles during runtime shutdown."""

        self._assert_generation(generation)
        with self._lock:
            evicted: list[ActorExecutionState] = []
            for session_id in tuple(self._actors):
                bucket = self._actors.get(session_id)
                if not bucket:
                    continue
                for actor_ref in tuple(bucket):
                    state = bucket[actor_ref]
                    self._discard_execution_session(state)
                    bucket.pop(actor_ref)
                    self._unbind_agent_session(state)
                    evicted.append(state)
                self._actors.pop(session_id, None)
            return tuple(evicted)

    def _assert_generation(self, generation: int) -> None:
        if type(generation) is not int or generation != self._writer_generation:
            raise StaleSessionGenerationError(
                "writer_generation_stale",
                "Gateway writer generation is stale",
            )

    def _require_session(self, session_id: str) -> GatewaySessionState:
        state = self._sessions.get(session_id)
        if state is None:
            raise SessionNotFoundError(session_id)
        return state

    @staticmethod
    def _assert_actor_conversation(
        parent: GatewaySessionState,
        actor: ActorExecutionState,
    ) -> None:
        principal = actor.principal
        expected_kind = normalize_chat_kind(
            parent.conversation.kind,
            parent.conversation.conversation_id,
        )
        actual_kind = normalize_chat_kind(
            principal.conversation.chat_kind,
            principal.conversation.chat_id,
        )
        if (
            parent.account.channel != principal.channel
            or parent.account.account_id != principal.account_id
            or principal.conversation.platform != parent.account.channel
            or expected_kind != actual_kind
            or parent.conversation.conversation_id != principal.conversation.chat_id
        ):
            raise ActorStateConflictError(
                "actor_conversation_mismatch",
                "Actor execution state belongs to another conversation",
            )

    @staticmethod
    def _validate_actor_update(
        current: ActorExecutionState,
        updated: ActorExecutionState,
    ) -> None:
        if current.key != updated.key or _principal_authority(current) != _principal_authority(
            updated
        ):
            raise ActorStateConflictError(
                "actor_identity_drift",
                "Actor execution identity cannot change in place",
            )
        if current.agent_session is not None and current.agent_session is not updated.agent_session:
            raise ActorStateConflictError(
                "agent_session_replacement_denied",
                "An actor Agent session must be evicted before replacement",
            )

    def _assert_agent_session_available(self, state: ActorExecutionState) -> None:
        session = state.agent_session
        if session is None:
            return
        if not callable(getattr(session, "discard", None)) and not callable(
            getattr(session, "close", None)
        ):
            raise ActorStateConflictError(
                "agent_session_not_discardable",
                "Actor Agent session must support discard or close",
            )
        owner = self._agent_session_owners.get(id(session))
        if owner is not None and owner != state.key:
            raise ActorStateConflictError(
                "agent_session_reused",
                "One Agent session cannot be shared across actors or Gateway sessions",
            )

    def _bind_agent_session(self, state: ActorExecutionState) -> None:
        if state.agent_session is not None:
            self._agent_session_owners[id(state.agent_session)] = state.key

    def _unbind_agent_session(self, state: ActorExecutionState) -> None:
        if state.agent_session is None:
            return
        owner = self._agent_session_owners.get(id(state.agent_session))
        if owner == state.key:
            self._agent_session_owners.pop(id(state.agent_session), None)

    @staticmethod
    def _discard_execution_session(state: ActorExecutionState) -> None:
        session = state.agent_session
        if session is None:
            return
        action = getattr(session, "discard", None)
        if not callable(action):
            action = getattr(session, "close", None)
        if not callable(action):
            raise ActorEvictionError(
                "agent_session_not_discardable",
                "Actor Agent session cannot be safely evicted",
            )
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - keep the state owned on cleanup failure
            raise ActorEvictionError(
                "agent_session_discard_failed",
                "Actor Agent session discard failed",
            ) from exc


def _conversation_key(state: GatewaySessionState) -> tuple[str, str, str, str]:
    conversation = state.conversation
    kind = normalize_chat_kind(conversation.kind, conversation.conversation_id)
    return (
        state.account.channel,
        state.account.account_id,
        str(kind or conversation.kind).strip().lower(),
        conversation.conversation_id,
    )


def _principal_authority(state: ActorExecutionState) -> tuple[str, ...]:
    principal = state.principal
    conversation = principal.conversation
    return (
        principal.channel,
        principal.account_id,
        conversation.platform,
        conversation.chat_kind,
        conversation.chat_id,
        principal.user_id,
        principal.role.value,
    )


__all__ = [
    "ActiveRunConflictError",
    "ActorEvictionError",
    "ActorStateConflictError",
    "SessionConflictError",
    "SessionManager",
    "SessionManagerError",
    "SessionNotFoundError",
    "StaleSessionGenerationError",
]
