"""Session-bound stdio MCP gateway for external main-agent backends."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chatcopilot.agent.backends.session_relay import call_session_relay

_LOGGER = logging.getLogger("chatcopilot.middleware.mcp.session_gateway")


def _load_config(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"session gateway config not found: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("unsupported session gateway config schema")
    allowed = payload.get("allowed_tools")
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ValueError("session gateway allowed_tools must be a string list")
    relay = payload.get("relay")
    if not isinstance(relay, dict) or not str(relay.get("token") or ""):
        raise ValueError("session gateway requires an authenticated relay")
    return payload


def _append_audit(config: dict[str, Any], payload: dict[str, Any]) -> None:
    raw_path = str(config.get("audit_path") or "").strip()
    if not raw_path:
        return
    path = Path(raw_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": str(config.get("session_id") or ""),
        "backend": "codex",
        **payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


async def _run(config_path: Path) -> int:
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import TextContent, Tool
    except ImportError as exc:
        sys.stderr.write(f"session MCP dependencies unavailable: {exc}\n")
        return 2

    config = _load_config(config_path)
    allowed = frozenset(str(item) for item in config["allowed_tools"])
    relay = dict(config["relay"])
    timeout_seconds = max(1.0, float(config.get("relay_timeout_seconds") or 30.0))
    listed = call_session_relay(
        relay,
        {"action": "list_tools"},
        timeout_seconds=timeout_seconds,
    )
    if not listed.get("ok") or not isinstance(listed.get("tools"), list):
        raise RuntimeError(str(listed.get("error") or "session relay tool listing failed"))
    selected = tuple(
        item
        for item in listed["tools"]
        if isinstance(item, dict) and str(item.get("name") or "") in allowed
    )
    selected_names = {str(tool["name"]) for tool in selected}
    unavailable = sorted(allowed - selected_names)
    if unavailable:
        _append_audit(
            config,
            {"event": "gateway_started", "unavailable_tools": unavailable},
        )
    server: Server = Server("chatcopilot-session-gateway")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=str(tool["name"]),
                description=str(tool.get("description") or ""),
                inputSchema=dict(tool.get("input_schema") or {}),
            )
            for tool in selected
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        if name not in selected_names:
            _append_audit(config, {"event": "tool_denied", "tool": name})
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "tool denied"}))]
        args = dict(arguments or {})
        _append_audit(config, {"event": "tool_started", "tool": name})
        response = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                call_session_relay,
                relay,
                {"action": "call_tool", "name": name, "arguments": args},
                timeout_seconds=timeout_seconds,
            ),
        )
        result = response.get("result") if response.get("ok") else None
        result_ok = bool(isinstance(result, dict) and result.get("ok"))
        _append_audit(
            config,
            {
                "event": "tool_finished",
                "tool": name,
                "ok": result_ok,
                "error_code": (
                    str(result.get("error_code") or "")
                    if isinstance(result, dict)
                    else str(response.get("error_code") or "relay_failed")
                ),
            },
        )
        payload = result if isinstance(result, dict) else response
        return [
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )
        ]

    _append_audit(
        config,
        {"event": "gateway_started", "allowed_tools": sorted(selected_names)},
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
    return 0


def serve(config_path: str | Path) -> int:
    try:
        return asyncio.run(_run(Path(config_path)))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        _LOGGER.exception("session MCP gateway crashed")
        return 1


__all__ = ["serve"]
