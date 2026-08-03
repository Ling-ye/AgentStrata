"""会话级文件回传 hook。

Agent 层禁止 import ``chatcopilot.platforms.*``，因此 ``send_files_to_user`` 工具
不能直接拿平台 sender。改由 middleware 在装配会话时注入一个 ``FileSender`` 回调
（绑定到当前 BotSpec 的平台 adapter），经 :class:`~chatcopilot.agent.tools.executor.ToolExecutor`
在执行 handler 期间通过 contextvar 暴露给 handler。

这与既有的 ``tool_payload_filter`` / ``background_submitter`` hook 同思路：策略由
middleware 注入，agent 不感知平台/角色概念。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Callable, Optional, Sequence


@dataclass(frozen=True)
class FileDeliveryResult:
    """一次文件回传的结果摘要，回灌给工具响应。"""

    sent_names: tuple[str, ...]
    sent_paths: tuple[str, ...]
    message: str = ""


# FileSender(files, message) -> FileDeliveryResult
# 由 middleware 绑定当前平台 adapter 后注入；负责把工作区文件回传到当前会话。
FileSender = Callable[[Sequence[str], str], FileDeliveryResult]


class FileDeliveryUnavailableError(RuntimeError):
    """当前会话未注入文件回传通道（平台不支持或未装配）。"""


_CURRENT_FILE_SENDER: contextvars.ContextVar[Optional[FileSender]] = contextvars.ContextVar(
    "chatcopilot_current_file_sender", default=None
)


def set_current_file_sender(sender: Optional[FileSender]) -> contextvars.Token:
    """设置当前执行上下文的 file sender，返回用于复位的 token。"""
    return _CURRENT_FILE_SENDER.set(sender)


def reset_current_file_sender(token: contextvars.Token) -> None:
    _CURRENT_FILE_SENDER.reset(token)


def get_current_file_sender() -> Optional[FileSender]:
    """取当前执行上下文注入的 file sender；未注入返回 ``None``。"""
    return _CURRENT_FILE_SENDER.get()


__all__ = [
    "FileDeliveryResult",
    "FileDeliveryUnavailableError",
    "FileSender",
    "get_current_file_sender",
    "reset_current_file_sender",
    "set_current_file_sender",
]
