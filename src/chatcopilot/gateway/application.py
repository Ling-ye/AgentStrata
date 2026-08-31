"""Durable Gateway session ownership shared by RPC and Channel ingress."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from typing import Final

from chatcopilot.application.sessions import (
    SessionManager,
    SessionManagerError,
)
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.gateway_rpc import SessionSnapshot
from chatcopilot.contracts.identity import ConversationIdentity, Role

from .server import GatewayClientContext, GatewayDispatchError
from .state_store import (
    GatewayStateStore,
    SessionConflict,
    SessionRecord,
    StaleWriterGeneration,
)


CLIENT_CHANNEL: Final = "gateway"


class GatewayApplicationError(RuntimeError):
    """Stable composition failure that never contains private state."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SessionPage:
    sessions: tuple[SessionRecord, ...]
    next_cursor: int | None


class GatewaySessionService:
    """Keep durable sessions and process-local execution state in one fenced generation."""

    def __init__(
        self,
        *,
        state_store: GatewayStateStore,
        session_manager: SessionManager,
        generation: int,
        client_roles: Mapping[str, Role] | None = None,
    ) -> None:
        if type(generation) is not int or generation < 1:
            raise ValueError("generation must be positive")
        if session_manager.writer_generation != generation:
            raise ValueError("SessionManager generation does not match Gateway generation")
        self.state_store = state_store
        self.session_manager = session_manager
        self.generation = generation
        self._client_roles = dict(client_roles or {})
        for client_id, role in self._client_roles.items():
            _validate_client_id(client_id)
            if not isinstance(role, Role):
                raise ValueError("client role values must be Role instances")
        self._hydrate()

    def assert_current_generation(self) -> None:
        if self.state_store.current_writer_generation() != self.generation:
            raise StaleWriterGeneration("Gateway writer generation is no longer current")

    def create_for_client(
        self,
        *,
        client: GatewayClientContext,
        session_id: str,
        mode: str,
        debug: bool,
    ) -> SessionRecord:
        account = client_account(client)
        conversation = ConversationRef(kind="p2p", conversation_id=session_id)
        return self._create(
            session_id=session_id,
            account=account,
            conversation=conversation,
            mode=mode,
            debug=debug,
        )

    def ensure_channel_session(
        self,
        *,
        account: ChannelAccountRef,
        conversation: ConversationRef,
    ) -> SessionRecord:
        self.assert_current_generation()
        existing = self.state_store.find_session_by_conversation(
            account=account,
            conversation=conversation,
        )
        if existing is not None:
            self._ensure_local(existing)
            return existing
        digest = hashlib.sha256(
            (
                account.channel
                + "\0"
                + account.account_id
                + "\0"
                + conversation.kind
                + "\0"
                + conversation.conversation_id
            ).encode("utf-8")
        ).hexdigest()[:32]
        return self._create(
            session_id="session_" + digest,
            account=account,
            conversation=conversation,
            mode="default",
            debug=False,
        )

    def get_visible(self, *, client: GatewayClientContext, session_id: str) -> SessionRecord:
        self.assert_current_generation()
        record = self.state_store.get_session(session_id)
        if record is None or not self.can_access(client=client, session=record):
            raise GatewayDispatchError("session_not_found", "Gateway session does not exist")
        return record

    def list_visible(
        self,
        *,
        client: GatewayClientContext,
        cursor: int,
        limit: int,
    ) -> SessionPage:
        self.assert_current_generation()
        if is_gateway_admin(client):
            rows = self.state_store.list_sessions(offset=cursor, limit=limit + 1)
            visible = rows[:limit]
            return SessionPage(
                sessions=visible,
                next_cursor=cursor + limit if len(rows) > limit else None,
            )

        # Storage pagination cannot leak global offsets. Scan until one full visible page
        # is collected and define the public cursor over the client's own ordered rows.
        account = client_account(client)
        visible_all: list[SessionRecord] = []
        offset = 0
        while True:
            rows = self.state_store.list_sessions(offset=offset, limit=1000)
            visible_all.extend(row for row in rows if row.account == account)
            if len(rows) < 1000:
                break
            offset += len(rows)
        page = tuple(visible_all[cursor : cursor + limit])
        next_cursor = cursor + limit if cursor + limit < len(visible_all) else None
        return SessionPage(sessions=page, next_cursor=next_cursor)

    def patch_visible(
        self,
        *,
        client: GatewayClientContext,
        session_id: str,
        mode: str | None,
        debug: bool | None,
    ) -> SessionRecord:
        current = self.get_visible(client=client, session_id=session_id)
        try:
            durable = self.state_store.patch_session(
                generation=self.generation,
                session_id=current.session_id,
                mode=mode,
                debug=debug,
            )
            self.session_manager.patch_session(
                session_id,
                generation=self.generation,
                mode=mode,
                debug=debug,
            )
        except (SessionConflict, SessionManagerError) as exc:
            raise GatewayApplicationError(
                "session_patch_conflict",
                "Gateway session could not be patched consistently",
            ) from exc
        return durable

    def update_event_cursor(self, *, session_id: str, event_cursor: int) -> SessionRecord:
        try:
            try:
                durable = self.state_store.patch_session(
                    generation=self.generation,
                    session_id=session_id,
                    event_cursor=event_cursor,
                )
            except SessionConflict:
                persisted = self.state_store.get_session(session_id)
                if persisted is None or persisted.event_cursor < event_cursor:
                    raise
                durable = persisted
            self.session_manager.patch_session(
                session_id,
                generation=self.generation,
                event_cursor=durable.event_cursor,
            )
        except (SessionConflict, SessionManagerError) as exc:
            raise GatewayApplicationError(
                "session_cursor_update_failed",
                "Gateway session cursor could not be updated consistently",
            ) from exc
        return durable

    def can_access(self, *, client: GatewayClientContext, session: SessionRecord) -> bool:
        return is_gateway_admin(client) or session.account == client_account(client)

    def principal_for_client(
        self,
        *,
        client: GatewayClientContext,
        session: SessionRecord,
    ) -> Principal:
        if not self.can_access(client=client, session=session):
            raise GatewayDispatchError("session_not_found", "Gateway session does not exist")
        if session.account.channel != CLIENT_CHANNEL:
            if not is_gateway_admin(client):
                raise GatewayDispatchError("session_not_found", "Gateway session does not exist")
            # Administrative visibility is not actor impersonation. An administrator can
            # inspect/control runs, but cannot send as a native Channel actor.
            raise GatewayDispatchError(
                "channel_actor_required",
                "Native Channel sessions accept messages only from authenticated Channel ingress",
            )
        role = self._client_roles.get(session.account.account_id, Role.USER)
        conversation = ConversationIdentity(
            platform=CLIENT_CHANNEL,
            chat_kind=session.conversation.kind,
            chat_id=session.conversation.conversation_id,
        )
        return Principal(
            channel=CLIENT_CHANNEL,
            account_id=session.account.account_id,
            conversation=conversation,
            user_id=session.account.account_id,
            role=role,
            evidence_digest=_client_evidence_digest(client, session),
        )

    def snapshot(self, record: SessionRecord) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=record.session_id,
            account=record.account,
            conversation=record.conversation,
            mode=record.mode,
            debug=record.debug,
            event_cursor=record.event_cursor,
            active_run_id=record.active_run_id,
        )

    def _create(
        self,
        *,
        session_id: str,
        account: ChannelAccountRef,
        conversation: ConversationRef,
        mode: str,
        debug: bool,
    ) -> SessionRecord:
        self.assert_current_generation()
        try:
            durable = self.state_store.create_session(
                generation=self.generation,
                session_id=session_id,
                account=account,
                conversation=conversation,
                mode=mode,
                debug=debug,
            )
            self._ensure_local(durable)
        except (SessionConflict, SessionManagerError) as exc:
            raise GatewayApplicationError(
                "session_create_conflict",
                "Gateway session could not be created consistently",
            ) from exc
        return durable

    def _hydrate(self) -> None:
        self.assert_current_generation()
        offset = 0
        while True:
            rows = self.state_store.list_sessions(offset=offset, limit=1000)
            for row in rows:
                self._ensure_local(row)
            if len(rows) < 1000:
                return
            offset += len(rows)

    def _ensure_local(self, record: SessionRecord) -> None:
        try:
            current = self.session_manager.get_session(record.session_id)
        except SessionManagerError:
            self.session_manager.create_session(
                session_id=record.session_id,
                account=record.account,
                conversation=record.conversation,
                generation=self.generation,
                mode=record.mode,
                debug=record.debug,
                event_cursor=record.event_cursor,
                active_run_id=record.active_run_id,
            )
            return
        if (
            current.account != record.account
            or current.conversation != record.conversation
            or current.active_run_id != record.active_run_id
        ):
            raise GatewayApplicationError(
                "session_state_drift",
                "Durable and process-local Gateway session state disagree",
            )
        if (
            current.mode != record.mode
            or current.debug != record.debug
            or current.event_cursor != record.event_cursor
        ):
            self.session_manager.patch_session(
                record.session_id,
                generation=self.generation,
                mode=record.mode,
                debug=record.debug,
                event_cursor=record.event_cursor,
            )


def client_account(client: GatewayClientContext) -> ChannelAccountRef:
    _validate_client_id(client.client_id)
    return ChannelAccountRef(CLIENT_CHANNEL, client.client_id)


def is_gateway_admin(client: GatewayClientContext) -> bool:
    return "gateway.admin" in client.scopes


def conversation_authority_ref(principal: Principal) -> str:
    conversation = principal.conversation
    return f"{conversation.platform}:{conversation.chat_kind}:{conversation.chat_id}"


def _client_evidence_digest(client: GatewayClientContext, session: SessionRecord) -> str:
    return "sha256:" + hashlib.sha256(
        (
            client.client_id
            + "\0"
            + client.client_mode
            + "\0"
            + session.session_id
            + "\0"
            + session.account.account_id
        ).encode("utf-8")
    ).hexdigest()


def _validate_client_id(client_id: str) -> None:
    if not isinstance(client_id, str) or not client_id or len(client_id) > 128:
        raise ValueError("client_id is invalid")


__all__ = [
    "CLIENT_CHANNEL",
    "GatewayApplicationError",
    "GatewaySessionService",
    "SessionPage",
    "client_account",
    "conversation_authority_ref",
    "is_gateway_admin",
]
