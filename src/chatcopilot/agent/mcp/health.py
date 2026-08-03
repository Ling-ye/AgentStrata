"""Structured MCP health/error feedback helpers."""
from __future__ import annotations

import json
from typing import Any

from chatcopilot.contracts.runtime import McpServerConfig

def _mcp_health_payload(
    *,
    error_code: str,
    server_id: str,
    tool_name: str,
    message: str,
    retryable: bool,
    timeout_seconds: float | None = None,
) -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "is_error": True,
        "error_code": error_code,
        "server_id": server_id,
        "tool_name": tool_name,
        "message": message,
        "retryable": retryable,
    }
    if timeout_seconds is not None:
        payload["timeout_seconds"] = int(timeout_seconds)
    return json.dumps(payload, ensure_ascii=False)


def _maybe_mcp_health_feedback(
    config: McpServerConfig, remote_name: str, text: str
) -> str | None:
    is_serialized_error = _serialized_result_is_error(text)
    if config.id != "xiaohongshu" and not is_serialized_error:
        return None
    error_code = _classify_mcp_error(config, text)
    if error_code == "mcp_error" and not is_serialized_error:
        return None
    return _mcp_health_payload(
        error_code=error_code,
        server_id=config.id,
        tool_name=remote_name,
        message=_compact_error_message(text),
        retryable=False,
    )


def _serialized_result_is_error(text: str) -> bool:
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and bool(data.get("is_error"))


def _classify_mcp_error(config: McpServerConfig, text: str) -> str:
    lowered = text.lower()
    if any(
        item in lowered
        for item in (
            "usage limit",
            "quota",
            "rate limit",
            "upgrade your plan",
            "plan's set usage limit",
        )
    ):
        return "mcp_quota_exceeded"
    if any(item in lowered for item in ("not connected", "connection refused", "brokenresourceerror")):
        return "mcp_unavailable"
    if config.id == "xiaohongshu":
        if any(item in text for item in ("未登录", "需要登录", "扫码", "二维码", "登录已失效")):
            return "xhs_login_required"
        if any(item in lowered for item in ("not logged in", "login required", "cookie")):
            return "xhs_login_required"
        if any(
            item in lowered
            for item in (
                "net::err_name_not_resolved",
                "navigation failed",
                "browser",
                "chromium",
                "rod",
                "panic",
                "page isn't available",
            )
        ):
            return "xhs_browser_error"
    return "mcp_error"


def _compact_error_message(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > 600:
        return text[:600] + "...[truncated]"
    return text


__all__ = ["_mcp_health_payload", "_maybe_mcp_health_feedback"]
