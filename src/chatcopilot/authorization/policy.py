"""Deterministic host policy for identity, admission, and role requirements."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Iterable

from chatcopilot.contracts.authorization import (
    AuthorizationDecision,
    AuthorizationRequest,
    Principal,
)
from chatcopilot.contracts.identity import Identity, Role, TurnIdentity, role_ge
from chatcopilot.core.allowlists import (
    NumericAllowlist,
    is_numeric_platform_id,
    parse_numeric_allowlist,
)
from chatcopilot.core.workspace_runtime.model import normalize_chat_kind


def _decision(
    request: AuthorizationRequest,
    *,
    allowed: bool,
    code: str,
    policy_version: str,
) -> AuthorizationDecision:
    digest = hashlib.sha256(
        (
            request.request_digest
            + "\0"
            + policy_version
            + "\0"
            + code
            + "\0"
            + str(allowed)
        ).encode("utf-8")
    ).hexdigest()[:24]
    return AuthorizationDecision(
        decision_id="authz_" + digest,
        request_id=request.request_id,
        request_digest=request.request_digest,
        allowed=allowed,
        code=code,
        policy_version=policy_version,
        actor_ref=request.principal.actor_ref,
    )


def make_authorization_decision(
    request: AuthorizationRequest,
    *,
    allowed: bool,
    code: str,
    policy_version: str,
) -> AuthorizationDecision:
    """Build the stable decision shape shared by all host policy modules."""

    return _decision(
        request,
        allowed=allowed,
        code=code,
        policy_version=policy_version,
    )


@dataclass(frozen=True)
class IdentityPolicy:
    owners: tuple[Identity, ...] = ()
    admins: tuple[Identity, ...] = ()

    @classmethod
    def from_iterables(
        cls,
        *,
        owners: Iterable[Identity] = (),
        admins: Iterable[Identity] = (),
    ) -> "IdentityPolicy":
        return cls(tuple(owners), tuple(admins))

    def principal(
        self,
        *,
        turn: TurnIdentity,
        channel: str,
        account_id: str,
        evidence_digest: str,
    ) -> Principal:
        # QQ display names are descriptive and never participate in role matching.
        allow_name_match = turn.conversation.platform.strip().lower() != "qq"
        matched_name = turn.sender_user_name if allow_name_match else None
        role = Role.USER
        for identity in self.owners:
            if identity.matches(user_id=turn.sender_user_id, user_name=matched_name):
                role = Role.OWNER
                break
        else:
            for identity in self.admins:
                if identity.matches(user_id=turn.sender_user_id, user_name=matched_name):
                    role = Role.ADMIN
                    break
        return Principal(
            channel=channel,
            account_id=account_id,
            conversation=turn.conversation,
            user_id=turn.sender_user_id,
            role=role,
            evidence_digest=evidence_digest,
        )


@dataclass(frozen=True)
class AdmissionPolicy:
    qq_users: NumericAllowlist
    qq_groups: NumericAllowlist
    policy_version: str

    @classmethod
    def from_raw(
        cls,
        *,
        qq_users: str | None,
        qq_groups: str | None,
        policy_version: str,
    ) -> "AdmissionPolicy":
        return cls(
            qq_users=parse_numeric_allowlist(qq_users, field="QQ_ALLOW_FROM"),
            qq_groups=parse_numeric_allowlist(qq_groups, field="QQ_ALLOW_GROUPS"),
            policy_version=policy_version,
        )

    def decide(self, request: AuthorizationRequest) -> AuthorizationDecision:
        if request.operation.value != "ingress":
            return _decision(
                request,
                allowed=False,
                code="authorization-operation-mismatch",
                policy_version=self.policy_version,
            )
        principal = request.principal
        conversation = principal.conversation
        if conversation.platform.strip().lower() != "qq":
            return _decision(
                request,
                allowed=True,
                code="platform-not-restricted",
                policy_version=self.policy_version,
            )
        sender = principal.user_id.strip()
        raw_kind = conversation.chat_kind.strip()
        kind = normalize_chat_kind(raw_kind, conversation.chat_id) if raw_kind else None
        if not is_numeric_platform_id(sender):
            code = "qq-sender-invalid"
        elif kind not in {"group", "p2p"}:
            code = "qq-chat-kind-invalid"
        elif kind == "group" and not is_numeric_platform_id(conversation.chat_id):
            code = "qq-group-invalid"
        elif kind == "group" and self.qq_users.allows(sender):
            return _decision(
                request,
                allowed=True,
                code="qq-group-user-allowed",
                policy_version=self.policy_version,
            )
        elif kind == "group" and self.qq_groups.allows(conversation.chat_id):
            return _decision(
                request,
                allowed=True,
                code="qq-group-allowed",
                policy_version=self.policy_version,
            )
        elif kind == "p2p" and self.qq_users.allows(sender):
            return _decision(
                request,
                allowed=True,
                code="qq-private-user-allowed",
                policy_version=self.policy_version,
            )
        elif kind == "group":
            code = "qq-group-not-allowed"
        else:
            code = "qq-private-user-not-allowed"
        return _decision(
            request,
            allowed=False,
            code=code,
            policy_version=self.policy_version,
        )


@dataclass(frozen=True)
class RolePolicy:
    policy_version: str

    def decide(
        self,
        request: AuthorizationRequest,
        *,
        required_role: Role = Role.USER,
        private_chat_only: bool = False,
    ) -> AuthorizationDecision:
        if not role_ge(request.principal.role, required_role):
            return _decision(
                request,
                allowed=False,
                code="required-role-not-met",
                policy_version=self.policy_version,
            )
        chat_kind = normalize_chat_kind(
            request.principal.conversation.chat_kind,
            request.principal.conversation.chat_id,
        )
        if private_chat_only and chat_kind != "p2p":
            return _decision(
                request,
                allowed=False,
                code="private-chat-required",
                policy_version=self.policy_version,
            )
        return _decision(
            request,
            allowed=True,
            code="allowed",
            policy_version=self.policy_version,
        )


__all__ = [
    "AdmissionPolicy",
    "IdentityPolicy",
    "RolePolicy",
    "make_authorization_decision",
]
