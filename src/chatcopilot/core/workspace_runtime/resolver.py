"""根据环境变量解析当前会话工作目录。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from chatcopilot.core.workspace_runtime.model import (
    Workspace,
    normalize_chat_kind,
)
from chatcopilot.project import ENV_PREFIX


def _sanitize_segment(value: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.@") else "_" for ch in value)
    return safe.strip("_") or "unknown"


def resolve_workspace(create: bool = True) -> Workspace:
    """根据环境变量解析当前会话工作目录。

    优先级（高到低）：
    1) ``CHATCOPILOT_WORKSPACE`` 显式整目录（调试用）
    2) ``WORKSPACE_ROOT`` + ``CHAT_KIND`` + ``USER_ID``（+ 群聊还要 ``CHAT_ID``）
    3) cwd fallback（开发态）
    """
    explicit = os.environ.get(f"{ENV_PREFIX}_WORKSPACE", "").strip()
    user_name = (os.environ.get(f"{ENV_PREFIX}_USER_NAME") or "").strip() or None
    if explicit:
        raw_chat_id = os.environ.get(f"{ENV_PREFIX}_CHAT_ID") or None
        chat_kind = normalize_chat_kind(os.environ.get(f"{ENV_PREFIX}_CHAT_KIND"), raw_chat_id)
        ws = Workspace(
            root=Path(explicit).expanduser().resolve(),
            chat_kind=chat_kind,
            chat_id=raw_chat_id,
            user_id=os.environ.get(f"{ENV_PREFIX}_USER_ID") or None,
            user_name=user_name,
        )
        return ws.ensure() if create else ws

    root_dir = os.environ.get(f"{ENV_PREFIX}_WORKSPACE_ROOT", "").strip()
    chat_id = (os.environ.get(f"{ENV_PREFIX}_CHAT_ID") or "").strip()
    chat_kind = normalize_chat_kind(
        os.environ.get(f"{ENV_PREFIX}_CHAT_KIND"),
        chat_id,
    ) or ""
    user_id = (os.environ.get(f"{ENV_PREFIX}_USER_ID") or "").strip()

    if root_dir:
        root_path = Path(root_dir).expanduser().resolve()
        target = _compose_target(root_path, chat_kind, chat_id, user_id)
        if target is not None:
            ws = Workspace(
                root=target,
                chat_kind=chat_kind or None,
                chat_id=chat_id or None,
                user_id=user_id or None,
                user_name=user_name,
            )
            return ws.ensure() if create else ws

    ws = Workspace(
        root=Path.cwd().resolve(),
        chat_kind=None,
        chat_id=None,
        user_id=None,
        user_name=user_name,
    )
    return ws.ensure() if create else ws


def _compose_target(
    root: Path,
    chat_kind: str,
    chat_id: str,
    user_id: str,
) -> Optional[Path]:
    """按租户身份算出工作目录子树。无法决策时返回 None 让上层走 fallback。"""
    if chat_kind == "p2p" and user_id:
        return root / f"p2p_{_sanitize_segment(user_id)}"
    if chat_kind == "group" and chat_id and user_id:
        return (
            root
            / f"group_{_sanitize_segment(chat_id)}"
            / f"user_{_sanitize_segment(user_id)}"
        )
    if chat_id:
        segment_kind = _sanitize_segment(chat_kind) if chat_kind else "chat"
        return root / f"{segment_kind}_{_sanitize_segment(chat_id)}"
    return root / "default"


def resolve_workspace_root(current: Optional[Workspace] = None) -> Path:
    """解析工作区总根目录（Owner 全局视图用）。"""
    raw_root = os.environ.get(f"{ENV_PREFIX}_WORKSPACE_ROOT", "").strip()
    if raw_root:
        return Path(raw_root).expanduser().resolve()
    ws = current if current is not None else resolve_workspace(create=False)
    return _infer_workspace_root(ws.root)


def _infer_workspace_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    name = root.name
    parent_name = root.parent.name
    if parent_name.startswith("group_") and name.startswith("user_"):
        return root.parent.parent
    if name.startswith("p2p_") or name == "default":
        return root.parent
    if "_" in name and (
        name.startswith("chat_")
        or name.startswith("group_")
        or name.startswith("p2p_")
    ):
        return root.parent
    return root


__all__ = ["resolve_workspace", "resolve_workspace_root"]
