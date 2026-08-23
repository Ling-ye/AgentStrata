"""Wrap remote MCP tool descriptors as local ToolDef handlers."""
from __future__ import annotations

import logging
import json
import re
from typing import Any, Dict

from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.agent.mcp.arguments import _normalize_mcp_tool_arguments
from chatcopilot.agent.mcp.errors import McpToolBusyError, McpToolTimeoutError
from chatcopilot.agent.mcp.health import (
    _classify_mcp_error,
    _mcp_health_payload,
    _maybe_mcp_health_feedback,
)

_LOGGER = logging.getLogger("chatcopilot.agent.mcp.client")
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_]")

def _wrap_remote_tool(
    config: McpServerConfig,
    provider: Any,
    remote_tool: Any,
    local_name: str,
) -> ToolDef:
    schema = getattr(remote_tool, "inputSchema", None) or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    remote_name = str(getattr(remote_tool, "name", ""))
    description = str(getattr(remote_tool, "description", "") or "")

    def _error_result(payload_text: str) -> ToolResult:
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError):
            payload = {"message": str(payload_text), "error_code": "mcp_error"}
        return ToolResult(
            ok=False,
            error=str(payload.get("message") or "MCP tool call failed"),
            error_code=str(payload.get("error_code") or "mcp_error"),
            stage="remote_call",
            data={
                "server_id": config.id,
                "tool_name": remote_name,
                "retryable": bool(payload.get("retryable", False)),
            },
        )

    def _handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx
        try:
            normalized_args = _normalize_mcp_tool_arguments(config, remote_name, args or {})
            text = provider.call_tool(config, remote_name, normalized_args)
        except ValueError as exc:
            return _error_result(
                _mcp_health_payload(
                    error_code="invalid_mcp_arguments",
                    server_id=config.id,
                    tool_name=remote_name,
                    message=str(exc),
                    retryable=False,
                )
            )
        except McpToolTimeoutError as exc:
            _LOGGER.warning("MCP tool handler timeout | %s/%s", config.id, remote_name)
            return _error_result(
                _mcp_health_payload(
                    error_code="mcp_timeout",
                    server_id=config.id,
                    tool_name=remote_name,
                    message=(
                        f"Tool '{remote_name}' on server '{config.id}' did not "
                        f"respond within {exc.timeout_seconds:.0f}s."
                    ),
                    retryable=False,
                    timeout_seconds=exc.timeout_seconds,
                )
            )
        except McpToolBusyError:
            _LOGGER.warning("MCP tool handler busy | %s/%s", config.id, remote_name)
            return _error_result(
                _mcp_health_payload(
                    error_code="mcp_busy",
                    server_id=config.id,
                    tool_name=remote_name,
                    message=(
                        f"Server '{config.id}' is busy with another call. "
                        f"Wait for it to finish before calling '{remote_name}' again."
                    ),
                    retryable=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("MCP tool handler error | %s/%s: %s", config.id, remote_name, exc)
            return _error_result(
                _mcp_health_payload(
                    error_code=_classify_mcp_error(config, str(exc)),
                    server_id=config.id,
                    tool_name=remote_name,
                    message=f"{type(exc).__name__}: {exc}",
                    retryable=False,
                )
            )
        feedback = _maybe_mcp_health_feedback(config, remote_name, text)
        if feedback is not None:
            return _error_result(feedback)
        summary = f"MCP {config.id}/{remote_name} returned:\n{text}"
        try:
            content = json.loads(text)
        except (TypeError, ValueError):
            content = text
        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "server_id": config.id,
                "tool_name": remote_name,
                "content": content,
            },
        )

    input_schema = dict(schema) if isinstance(schema, dict) else {}
    input_schema.setdefault("type", "object")
    input_schema.setdefault("properties", properties if isinstance(properties, dict) else {})
    input_schema.setdefault(
        "required",
        [str(item) for item in required] if isinstance(required, list) else [],
    )

    return ToolDef(
        name=local_name,
        summary=(
            f"[MCP:{config.id}] {description}"
            if description
            else f"[MCP:{config.id}] Remote MCP tool {remote_name}"
        ),
        input_schema=input_schema,
        output_schema=object_schema(
            {
                "server_id": {"type": "string"},
                "tool_name": {"type": "string"},
                "content": {},
            },
            required=("server_id", "tool_name", "content"),
        ),
        handler=_handler,
        requires_role="owner" if config.risk == "write" else None,
        category="mcp",
        owner=config.id,
        module="chatcopilot.agent.mcp.client",
        artifact_kinds=(),
        metadata={
            "mcp_server_id": config.id,
            "mcp_remote_name": remote_name,
            "mcp_exposure": config.exposure,
            "mcp_allowed_subagents": list(config.allowed_subagents),
            "mcp_allowed_tools": list(config.allowed_tools),
            "mcp_denied_tools": list(config.denied_tools),
            "mcp_risk": config.risk,
            "mcp_search_only_tools": list(config.search_only_tools),
        },
    )


def _safe_tool_name(value: str) -> str:
    name = _TOOL_NAME_RE.sub("_", value.strip())
    name = re.sub(r"_+", "_", name).strip("_")
    if name and name[0].isdigit():
        name = f"mcp_{name}"
    return name


__all__ = ["_safe_tool_name", "_wrap_remote_tool"]
