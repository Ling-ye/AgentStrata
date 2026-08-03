"""Stateless HTTP MCP request/response helpers."""
from __future__ import annotations

import json
import types
import urllib.error
import urllib.request
from typing import Any, Dict

from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.agent.mcp.serialization import _compact_mcp_response

def _stateless_list_tools(config: McpServerConfig) -> list[Any]:
    payload = _stateless_rpc(
        config,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    result = payload.get("result") if isinstance(payload, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else []
    out: list[Any] = []
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        schema = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {}
        name = str(item.get("name") or "")
        if not name:
            continue
        out.append(
            types.SimpleNamespace(
                name=name,
                description=str(item.get("description") or ""),
                inputSchema=schema,
            )
        )
    return out


def _stateless_call_tool(config: McpServerConfig, name: str, arguments: Dict[str, Any]) -> str:
    payload = _stateless_rpc(
        config,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("stateless MCP response missing result")
    return _serialize_stateless_result(result, max_chars=config.max_result_chars)


def _stateless_rpc(config: McpServerConfig, payload: dict[str, Any]) -> dict[str, Any]:
    if not config.url:
        raise ValueError("stateless HTTP MCP server requires url")
    request = urllib.request.Request(
        config.url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(config.headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(config.timeout_seconds))) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:800]}") from exc
    data = _parse_stateless_response(raw)
    if data.get("error"):
        raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
    return data


def _parse_stateless_response(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    if not stripped:
        raise RuntimeError("empty stateless MCP response")

    objects: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    if objects:
        return objects[-1]
    if not stripped.startswith(("{", "[")):
        sample = stripped[:160].replace("\n", " ")
        raise RuntimeError(f"non-JSON stateless MCP response: {sample}")
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("response JSON is not an object")
    return parsed


def _serialize_stateless_result(result: dict[str, Any], *, max_chars: int) -> str:
    payload: dict[str, Any] = {
        "is_error": bool(result.get("isError") or result.get("is_error")),
    }
    structured = result.get("structuredContent")
    if structured is not None:
        payload["structured"] = structured
    content_items: list[dict[str, Any]] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "text":
            content_items.append({"type": "text", "text": str(item.get("text") or "")})
        else:
            content_items.append({"type": item_type or "unknown"})
    if content_items:
        payload["content"] = content_items
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > max_chars:
        text = _compact_mcp_response(payload, max_chars)
    return text


__all__ = [
    "_parse_stateless_response",
    "_serialize_stateless_result",
    "_stateless_call_tool",
    "_stateless_list_tools",
    "_stateless_rpc",
]
