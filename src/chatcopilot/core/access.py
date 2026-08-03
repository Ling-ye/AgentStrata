"""Platform-neutral role and access policy.

机器人三档用户权限解析。

三种角色：

- **OWNER**：私聊中可切换通用模式与调试模式。身份必须由部署环境显式配置。
- **ADMIN**：暂同 USER 行为，但保留独立 Role 枚举与配置入口，未来差分。
- **USER**：普通用户。不能获知代码结构 / 代码细节 / 内部目录 / 部署架构。

身份匹配：

- ``Identity`` 同时支持 ``name``（飞书显示名）与 ``user_id``（open_id），任一字段命中
  即识别为对应角色。``user_id`` 命中**优先**，因为飞书显示名可改也可能重名。
- 公开源码不携带任何内置 Owner 或 Admin 身份。
- env 显式配置角色身份：
  - ``CHATCOPILOT_ADD_OWNER_IDS="ou_a,ou_b"``
  - ``CHATCOPILOT_ADD_OWNER_NAMES="另一个人"``（罕用）
  - ``CHATCOPILOT_ADD_ADMIN_IDS`` / ``CHATCOPILOT_ADD_ADMIN_NAMES``

匹配优先级：``OWNER > ADMIN > USER``。同一身份若同时在 owners/admins 配置里出现，
按 OWNER 解释（高位优先）。
"""
from __future__ import annotations

import os
from typing import List, Optional

from chatcopilot.contracts.identity import AssistantMode, Identity, Role, role_ge
from chatcopilot.project import ENV_PREFIX


def default_debug_mode(role: Role) -> bool:
    """All sessions start in final-answer-only mode; debug is explicit opt-in."""
    return False


def default_assistant_mode(role: Role) -> AssistantMode:
    """All roles start in the performance-only business mode."""
    return AssistantMode.PERFORMANCE


def normalize_chat_kind(
    chat_kind: Optional[str],
    chat_id: Optional[str] = None,
) -> Optional[str]:
    """归一化飞书会话类型。

    cc-connect / 飞书能明确给出消息类型时优先信任它；私聊也可能携带 ``oc_`` chat_id，
    不能仅凭 ``oc_`` 把会话改判成群聊。
    """
    normalized_kind = (chat_kind or "").strip().lower().replace("-", "_")
    if "p2p" in normalized_kind or normalized_kind in {"private", "direct", "single"}:
        return "p2p"
    if "group" in normalized_kind or normalized_kind in {"chat", "room"}:
        return "group"

    if not normalized_kind:
        # Feishu private chats may still include an oc_* chat_id. Treat missing
        # chat kind as p2p unless cc-connect gives an explicit group signal.
        return "p2p"
    return normalized_kind or None


def _is_owner_p2p(
    role: Role,
    chat_kind: Optional[str],
    chat_id: Optional[str] = None,
) -> bool:
    """仅私聊 Owner 拥有扩展会话能力。"""
    return role == Role.OWNER and normalize_chat_kind(chat_kind, chat_id) == "p2p"


def can_select_general_mode(
    role: Role,
    chat_kind: Optional[str],
    chat_id: Optional[str] = None,
) -> bool:
    """是否允许从性能分析模式切到通用模式。

    群聊中任何身份都固定为性能分析模式；只有私聊 Owner 可切通用模式。
    """
    return _is_owner_p2p(role, chat_kind, chat_id)


def can_toggle_debug(
    role: Role,
    chat_kind: Optional[str],
    chat_id: Optional[str] = None,
) -> bool:
    """是否允许通过 ``/debug on/off`` 或 ACP ``set_session_mode`` 切换调试模式。

    群聊中任何身份都固定关闭 debug；只有私聊 Owner 可临时开启。
    """
    return _is_owner_p2p(role, chat_kind, chat_id)


# ----------------------------------------------------------------------------
# 默认配置（公开源码中不得放真实平台身份）
# ----------------------------------------------------------------------------
DEFAULT_OWNERS: List[Identity] = []
DEFAULT_ADMINS: List[Identity] = []


# ----------------------------------------------------------------------------
# env 覆盖解析
# ----------------------------------------------------------------------------
def _load_extra(env_var: str, *, is_user_id: bool) -> List[Identity]:
    """从 env 读逗号分隔的列表，组装成 Identity 数组。

    Args:
        env_var: 环境变量名，如 ``CHATCOPILOT_ADD_OWNER_IDS``。
        is_user_id: True → 字符串作为 ``user_id`` 字段；False → 作为 ``name`` 字段。
    """
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        return []
    out: List[Identity] = []
    for token in raw.split(","):
        value = token.strip()
        if not value:
            continue
        if is_user_id:
            out.append(Identity(user_id=value))
        else:
            out.append(Identity(name=value))
    return out


def _collect(
    defaults: List[Identity],
    *,
    add_ids_env: str,
    add_names_env: str,
) -> List[Identity]:
    """合并默认配置 + env 追加项，过滤掉无效项（两字段都空的）。"""
    merged: List[Identity] = []
    for ident in defaults:
        if ident.is_valid():
            merged.append(ident)
    merged.extend(_load_extra(add_ids_env, is_user_id=True))
    merged.extend(_load_extra(add_names_env, is_user_id=False))
    return merged


def get_owners() -> List[Identity]:
    """当前生效的 Owner 列表。"""
    return _collect(
        DEFAULT_OWNERS,
        add_ids_env=f"{ENV_PREFIX}_ADD_OWNER_IDS",
        add_names_env=f"{ENV_PREFIX}_ADD_OWNER_NAMES",
    )


def get_admins() -> List[Identity]:
    """当前生效的 Admin 列表。"""
    return _collect(
        DEFAULT_ADMINS,
        add_ids_env=f"{ENV_PREFIX}_ADD_ADMIN_IDS",
        add_names_env=f"{ENV_PREFIX}_ADD_ADMIN_NAMES",
    )


# ----------------------------------------------------------------------------
# 主入口：根据 user_id + user_name 解析角色
# ----------------------------------------------------------------------------
def resolve_role(
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    *,
    allow_name_match: bool = True,
) -> Role:
    """把当前会话的平台身份映射成 Role。

    匹配顺序：OWNER → ADMIN → USER fallback。
    ``user_id`` 始终参与精确匹配；``allow_name_match=False`` 时完全忽略显示名。
    user_id 与 user_name 都为空时，仍 fallback 到 USER（不会意外升权）。
    """
    matched_name = user_name if allow_name_match else None
    for ident in get_owners():
        if ident.matches(user_id=user_id, user_name=matched_name):
            return Role.OWNER
    for ident in get_admins():
        if ident.matches(user_id=user_id, user_name=matched_name):
            return Role.ADMIN
    return Role.USER


__all__ = [
    "Role",
    "AssistantMode",
    "Identity",
    "role_ge",
    "default_assistant_mode",
    "default_debug_mode",
    "can_select_general_mode",
    "can_toggle_debug",
    "normalize_chat_kind",
    "DEFAULT_OWNERS",
    "DEFAULT_ADMINS",
    "get_owners",
    "get_admins",
    "resolve_role",
]
