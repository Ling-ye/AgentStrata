"""Workspace file delivery handler."""
from __future__ import annotations

from typing import Any, Dict

from chatcopilot.external_tools.shared.tool_spec import HandlerResult

def _handler_send_files_to_user(args: Dict[str, Any]) -> HandlerResult:
    """把当前用户工作区内的文件回传到当前会话。

    平台无关：实际回传通道由 middleware 在装配会话时注入（绑定当前 BotSpec 的平台
    adapter），通过 ``file_delivery`` 的 contextvar hook 拿到，避免 agent 层直接 import
    任何 ``chatcopilot.platforms.*``。
    """
    from chatcopilot.agent.tools.file_delivery import (
        FileDeliveryUnavailableError,
        get_current_file_sender,
    )

    raw_files = args.get("files")
    if not isinstance(raw_files, (list, tuple)) or not raw_files:
        raise ValueError("缺少必填参数: files (非空数组)")
    message = str(args.get("message") or "").strip()

    sender = get_current_file_sender()
    if sender is None:
        raise FileDeliveryUnavailableError(
            "当前会话未注入文件回传通道（平台不支持或未装配），无法发送文件。"
        )

    result = sender(list(raw_files), message)
    names = ", ".join(result.sent_names)
    msg_hint = f"，附言: {message[:60]}" if message else ""
    return (
        f"已发送 {len(result.sent_paths)} 个文件到当前会话：{names}{msg_hint}",
        list(result.sent_paths),
        None,
    )


__all__ = ["_handler_send_files_to_user"]
