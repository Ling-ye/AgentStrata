"""MCP remote-tool argument normalization."""
from __future__ import annotations

from typing import Any, Dict

from chatcopilot.contracts.runtime import McpServerConfig

def _normalize_mcp_tool_arguments(
    config: McpServerConfig, remote_name: str, args: Dict[str, Any]
) -> Dict[str, Any]:
    if config.id == "playwright":
        return _normalize_playwright_arguments(remote_name, args)
    if config.id == "git":
        return _normalize_git_arguments(remote_name, args)
    if config.id != "xiaohongshu" or remote_name != "search_feeds":
        return args
    out = dict(args)
    filters = out.get("filters")
    if isinstance(filters, dict):
        defaults = {
            "sort_by": "综合",
            "note_type": "不限",
            "publish_time": "不限",
            "search_scope": "不限",
            "location": "不限",
        }
        cleaned = {
            str(key): value
            for key, value in filters.items()
            if value not in ("", None) and defaults.get(str(key)) != value
        }
        if cleaned:
            out["filters"] = cleaned
        else:
            out.pop("filters", None)
    return out


def _normalize_git_arguments(remote_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from chatcopilot.external_tools.dev.git_guard import ensure_agentic_commit_message

    out = dict(args)
    if remote_name == "git_commit" and "message" in out:
        out["message"] = ensure_agentic_commit_message(str(out["message"]))
    return out


def _normalize_playwright_arguments(
    remote_name: str, args: Dict[str, Any]
) -> Dict[str, Any]:
    out = dict(args)
    if remote_name == "browser_navigate":
        from chatcopilot.external_tools.web_fetch.tools import _validate_url

        out["url"] = _validate_url(str(out.get("url") or ""))
    elif remote_name == "browser_snapshot":
        if out.get("filename"):
            raise ValueError("browser_snapshot filename is not allowed")
        out.pop("boxes", None)
    elif remote_name == "browser_tabs":
        action = str(out.get("action") or "")
        if action not in {"list", "select", "close"}:
            raise ValueError("browser_tabs only allows list, select, or close")
        out.pop("url", None)
    elif remote_name == "browser_press_key":
        key = str(out.get("key") or "")
        if key not in {"PageDown", "PageUp", "Home", "End", "Escape"}:
            raise ValueError("browser_press_key only allows navigation keys")
    elif remote_name == "browser_wait_for" and "time" in out:
        try:
            wait_seconds = float(out["time"])
        except (TypeError, ValueError) as exc:
            raise ValueError("browser_wait_for time must be numeric") from exc
        out["time"] = min(10, max(0, wait_seconds))
    return out


__all__ = ["_normalize_mcp_tool_arguments"]
