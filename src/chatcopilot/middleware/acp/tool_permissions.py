"""Adapt the Legacy trusted identity to the canonical host tool policy."""

from __future__ import annotations

from typing import Any
from chatcopilot.application.tool_authorization import build_tool_permission_filter
from chatcopilot.authorization.tools import member_safe_tool, owner_project_access
from chatcopilot.contracts.authorization import Principal
from chatcopilot.contracts.identity import ConversationIdentity, Role, role_value


def build_permission_filter(
    role: Any, workspace: Any = None, *, platform: str = "legacy", account_id: str = "local"
):
    principal = Principal(
        channel=platform,
        account_id=account_id,
        conversation=ConversationIdentity(
            platform=platform,
            chat_kind=getattr(workspace, "chat_kind", "p2p") or "p2p",
            chat_id=str(getattr(workspace, "chat_id", "") or "local"),
        ),
        user_id=str(getattr(workspace, "user_id", "") or "local"),
        role=Role(role_value(role)),
        evidence_digest="trusted-legacy-entry",
    )
    return build_tool_permission_filter(principal, policy_version="runtime-access-v2")


__all__ = ["build_permission_filter", "member_safe_tool", "owner_project_access"]
