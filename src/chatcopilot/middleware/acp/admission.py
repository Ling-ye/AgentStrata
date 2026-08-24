"""Authoritative QQ admission decision at the ACP boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from chatcopilot.core.allowlists import is_numeric_platform_id, parse_numeric_allowlist
from chatcopilot.core.workspace_runtime.model import normalize_chat_kind

QQ_USER_ALLOWLIST_ENV = "QQ_ALLOW_FROM"
QQ_GROUP_ALLOWLIST_ENV = "QQ_ALLOW_GROUPS"


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    code: str


def evaluate_admission(
    *,
    platform: str,
    chat_kind: str | None,
    chat_id: str | None,
    sender_id: str | None,
    env: Mapping[str, str] | None = None,
) -> AdmissionDecision:
    """Decide whether one identity-verified turn may enter AgentStrata."""

    if str(platform or "").strip().lower() != "qq":
        return AdmissionDecision(True, "platform-not-restricted")

    resolved_env = os.environ if env is None else env
    users = parse_numeric_allowlist(
        resolved_env.get(QQ_USER_ALLOWLIST_ENV),
        field=QQ_USER_ALLOWLIST_ENV,
    )
    groups = parse_numeric_allowlist(
        resolved_env.get(QQ_GROUP_ALLOWLIST_ENV),
        field=QQ_GROUP_ALLOWLIST_ENV,
    )
    sender = str(sender_id or "").strip()
    raw_kind = str(chat_kind or "").strip()
    if not raw_kind:
        return AdmissionDecision(False, "qq-chat-kind-invalid")
    kind = normalize_chat_kind(raw_kind, None)

    if not is_numeric_platform_id(sender):
        return AdmissionDecision(False, "qq-sender-invalid")
    if kind not in {"group", "p2p"}:
        return AdmissionDecision(False, "qq-chat-kind-invalid")
    if kind == "group":
        conversation = str(chat_id or "").strip()
        if not is_numeric_platform_id(conversation):
            return AdmissionDecision(False, "qq-group-invalid")
        if users.allows(sender):
            return AdmissionDecision(True, "qq-group-user-allowed")
        if groups.allows(conversation):
            return AdmissionDecision(True, "qq-group-allowed")
        return AdmissionDecision(False, "qq-group-not-allowed")
    if users.allows(sender):
        return AdmissionDecision(True, "qq-private-user-allowed")
    return AdmissionDecision(False, "qq-private-user-not-allowed")


__all__ = [
    "AdmissionDecision",
    "QQ_GROUP_ALLOWLIST_ENV",
    "QQ_USER_ALLOWLIST_ENV",
    "evaluate_admission",
]
