"""Dev lifecycle tools for source-first self update publication."""
from __future__ import annotations

from typing import Any

from chatcopilot.external_tools.dev.self_update_publisher import (
    SelfUpdatePublishRequest,
    build_publish_request_from_env,
    publish_self_update,
)
from chatcopilot.external_tools.shared.tool_spec import HandlerResult


def execute_finalize_self_update_from_workspace(
    args: dict[str, Any],
    *,
    workspace_payload: dict[str, Any],
    session_id: str | None = None,
) -> HandlerResult:
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ValueError("reason is required")
    request = build_publish_request_from_env(
        reason=reason,
        workspace_payload=workspace_payload,
        session_id=session_id,
    )
    return execute_finalize_self_update_request(request)


def execute_finalize_self_update_request(request: SelfUpdatePublishRequest) -> HandlerResult:
    result = publish_self_update(request)
    return result.summary, result.outputs, None


__all__ = [
    "execute_finalize_self_update_from_workspace",
    "execute_finalize_self_update_request",
]
