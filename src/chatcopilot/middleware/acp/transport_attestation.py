"""QQ group transport-attestation policy over the core session store."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from chatcopilot.contracts.identity import TurnIdentity
from chatcopilot.contracts.session_attestation import SessionAttestationResultKind
from chatcopilot.core.session_env_store import (
    SessionEnvSecurityError,
    consume_session_attestation,
    session_env_path_from_environment,
)


class TransportAttestationError(ValueError):
    """A QQ group sender envelope is not backed by the private transport hook."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TransportAttestationValidation:
    """Successful transport actor binding plus optional body-digest evidence."""

    content_digest_matches: bool


def validate_qq_group_transport_attestation(
    identity: TurnIdentity,
    clean_text: str,
    *,
    require_content_digest: bool = True,
) -> TransportAttestationValidation | None:
    """Consume the one hook record that matches the authenticated QQ group turn."""

    conversation = identity.conversation
    if conversation.platform != "qq" or conversation.chat_kind != "group":
        return None
    path = session_env_path_from_environment()
    if path is None:
        raise TransportAttestationError(
            "qq_transport_attestation_missing",
            "QQ 群消息缺少可信的传输身份记录，已拒绝处理。",
        )
    expected_digest = hashlib.sha256((clean_text or "").strip().encode("utf-8")).hexdigest()
    try:
        result = consume_session_attestation(
            path,
            transport_user_id=identity.sender_user_id,
            content_sha256=expected_digest,
        )
    except (OSError, SessionEnvSecurityError) as exc:
        raise TransportAttestationError(
            "qq_transport_attestation_unsafe",
            "QQ 群消息的传输身份记录不安全或不可用，已拒绝处理。",
        ) from exc
    if result.kind is SessionAttestationResultKind.MATCHED:
        return TransportAttestationValidation(content_digest_matches=True)
    if result.kind is SessionAttestationResultKind.MISSING:
        raise TransportAttestationError(
            "qq_transport_attestation_missing",
            "QQ 群消息缺少当前入站事件的可信身份记录，已拒绝处理。",
        )
    if result.kind is SessionAttestationResultKind.ACTOR_MISMATCH:
        raise TransportAttestationError(
            "qq_transport_actor_mismatch",
            "QQ 群消息发送者与独立传输身份不一致，已拒绝处理。",
        )
    if require_content_digest:
        raise TransportAttestationError(
            "qq_transport_content_mismatch",
            "QQ 群消息正文与独立传输记录不一致，已拒绝处理。",
        )
    return TransportAttestationValidation(content_digest_matches=False)


__all__ = [
    "SessionEnvSecurityError",
    "TransportAttestationError",
    "TransportAttestationValidation",
    "validate_qq_group_transport_attestation",
]
