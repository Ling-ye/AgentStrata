"""Shared helpers for workspace tool handlers."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from chatcopilot.agent.tools.workspace_context import cleanup_workspace

def _require(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or value == "":
        raise ValueError(f"缺少必填参数: {key}")
    return str(value)


def _silent_cleanup(ws: Any) -> None:
    try:
        cleanup_workspace(ws)
    except Exception:  # noqa: BLE001
        pass


def _is_unsafe_member(member_name: str) -> bool:
    if not member_name:
        return False
    if member_name.startswith("/") or member_name.startswith("\\"):
        return True
    parts = Path(member_name).parts
    return any(part == ".." for part in parts)


def _format_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num} B"


def _format_mtime(value: Any) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


__all__ = ["_format_bytes", "_format_mtime", "_is_unsafe_member", "_require", "_silent_cleanup"]
