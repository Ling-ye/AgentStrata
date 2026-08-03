"""按 ``BotSpec.platform.type`` 分发平台能力的门面。

平台知识聚合在每个 ``platforms/<name>/adapter.py`` 的 :class:`PlatformAdapter`
上，由 ``platforms.registry`` 目录扫描自动发现。本模块是 middleware / agent 取用
平台能力的稳定门面：

- ``get_adapter`` 是主 API，返回当前实例对应的 :class:`PlatformAdapter`。
- ``supports_*`` 是 adapter 之上的便捷包装。
- ``get_sender`` / ``get_notifier`` 仍返回平台的 sender / notifier **模块**（保持
  ``JobDispatcher`` 等历史调用点零改动）；模块由 ``platforms/<name>/`` 约定提供。

middleware 与 agent 层不直接 import 任何具体平台模块；``BotSpec.platform.type``
就是唯一开关。新增平台只需加 ``platforms/<name>/adapter.py``，本模块无需改动。
"""

from __future__ import annotations

import importlib
from typing import Any, Mapping

from chatcopilot.platforms.base import PlatformAdapter, SessionIdentity
from chatcopilot.platforms.registry import (
    UnsupportedPlatformError,
    get_adapter,
    is_supported,
    supported_platform_types,
)


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------
def supports_role_matrix(platform_type: str) -> bool:
    """该平台是否启用 Owner / Admin / User 角色矩阵 + 业务模式切换。"""
    return get_adapter(platform_type).supports_role_matrix


def supports_user_files_pipeline(platform_type: str) -> bool:
    """该平台是否启用“私聊文件上传 → per-user 私人空间”附件流水线。"""
    return get_adapter(platform_type).supports_user_files_pipeline


def supports_background_jobs(platform_type: str) -> bool:
    """该平台是否能投递后台任务完成通知。"""
    return get_adapter(platform_type).supports_background_jobs


# ---------------------------------------------------------------------------
# Access gate helpers
# ---------------------------------------------------------------------------
def detect_self_mention(
    platform_type: str,
    text: str,
    *,
    env: Mapping[str, str],
    mention_name: str | None = None,
) -> bool | None:
    """委托当前平台 adapter 判断消息是否 @ 了本机器人（供群聊门禁使用）。"""
    return get_adapter(platform_type).detect_self_mention(
        text, env=env, mention_name=mention_name
    )


def parse_session_identity(
    platform_type: str,
    *,
    session_key: str,
    hook_user_id: str | None = None,
    hook_chat_id: str | None = None,
    hook_chat_kind: str | None = None,
    hook_user_name: str | None = None,
) -> SessionIdentity:
    """委托当前平台 adapter 解析 cc-connect 会话身份。"""
    return get_adapter(platform_type).parse_session_identity(
        session_key=session_key,
        hook_user_id=hook_user_id,
        hook_chat_id=hook_chat_id,
        hook_chat_kind=hook_chat_kind,
        hook_user_name=hook_user_name,
    )


# ---------------------------------------------------------------------------
# Module factories（保持历史调用点签名：返回 sender / notifier 模块）
# ---------------------------------------------------------------------------
def get_sender(platform_type: str) -> Any:
    """返回平台 sender 模块（约定暴露 ``resolve_sendable_paths`` / ``send_via_cc_connect``）。"""
    return importlib.import_module(f"chatcopilot.platforms.{get_adapter(platform_type).name}.sender")


def get_notifier(platform_type: str) -> Any:
    """返回平台 notifier 模块（约定暴露 ``resolve_delivery_target`` / ``send_text_to_workspace``）。"""
    return importlib.import_module(f"chatcopilot.platforms.{get_adapter(platform_type).name}.notifier")


__all__ = [
    "PlatformAdapter",
    "UnsupportedPlatformError",
    "detect_self_mention",
    "get_adapter",
    "get_notifier",
    "get_sender",
    "is_supported",
    "parse_session_identity",
    "supported_platform_types",
    "supports_background_jobs",
    "supports_role_matrix",
    "supports_user_files_pipeline",
]
