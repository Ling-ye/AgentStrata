"""Trusted Owner-only persona mutations behind the persona tool boundary."""
from __future__ import annotations

import hashlib

from chatcopilot.contracts.identity import role_value
from chatcopilot.contracts.persona_control import (
    PersonaMutationReceipt,
    PersonaMutationRequest,
)
from chatcopilot.contracts.persistent_state import PersistentConversationState
from chatcopilot.contracts.workspace import normalize_chat_kind


class PersonaControlService:
    """Apply one path-free mutation to the current trusted persistent state."""

    def __init__(
        self,
        *,
        persistent_state: PersistentConversationState,
        caller_role: object,
        chat_kind: str | None,
    ) -> None:
        self._state = persistent_state
        self._caller_role = role_value(caller_role)
        self._chat_kind = normalize_chat_kind(chat_kind)

    def resolve_scope(self, scope: str) -> str:
        normalized = (scope or "default").strip().lower()
        if normalized == "default":
            return "group" if self._chat_kind == "group" else "user"
        if normalized not in {"global", "group", "user"}:
            raise ValueError("persona scope must be default/global/group/user")
        if normalized == "group" and self._chat_kind != "group":
            raise ValueError("group persona is only valid in a group conversation")
        if normalized == "user" and self._chat_kind == "group":
            raise ValueError("user persona is unavailable in a group conversation")
        return normalized

    def execute(self, request: PersonaMutationRequest) -> PersonaMutationReceipt:
        operation = (request.operation or "").strip().lower()
        try:
            if self._caller_role != "owner":
                raise PermissionError("persona configuration is Owner-only")
            scope = self.resolve_scope(request.scope)
            if operation == "set":
                self._state.persona_set(scope, request.text)
            elif operation == "clear":
                if not request.confirm:
                    raise ValueError("persona clear requires explicit confirmation")
                self._state.persona_clear(scope)
            else:
                raise ValueError("unsupported persona mutation operation")
            snapshot = self._state.persona_snapshot(scope)
            return PersonaMutationReceipt(
                ok=True,
                operation=operation,
                scope=scope,
                content_sha256=hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
            )
        except PermissionError:
            return PersonaMutationReceipt(
                ok=False,
                operation=operation,
                scope=(request.scope or "default").strip().lower(),
                error_code="persona_owner_required",
            )
        except ValueError:
            return PersonaMutationReceipt(
                ok=False,
                operation=operation,
                scope=(request.scope or "default").strip().lower(),
                error_code="persona_request_invalid",
            )
        except Exception:  # noqa: BLE001 - never convert a failed write to success
            return PersonaMutationReceipt(
                ok=False,
                operation=operation,
                scope=(request.scope or "default").strip().lower(),
                error_code="persona_persistence_failed",
            )


__all__ = ["PersonaControlService"]
