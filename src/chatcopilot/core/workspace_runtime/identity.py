"""Workspace 身份元数据 IDENTITY.json 的持久化与读取。"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

from chatcopilot.contracts.workspace import (
    IDENTITY_FILENAME,
    WORKSPACE_SCOPE_GROUP_SHARED,
    WorkspaceView,
)

_LOGGER = logging.getLogger("chatcopilot.core.workspace_runtime.identity")


def persist_workspace_identity(ws: WorkspaceView) -> None:
    """把当前会话身份写入工作区，供 Owner 全局扫描读取显示名。

    任意 user_id / user_name / chat 信息存在时才写。失败只记日志，不阻断主流程。
    """
    if ws.scope == WORKSPACE_SCOPE_GROUP_SHARED:
        return
    shared_group = False
    payload = {
        "schema_version": 2,
        "scope": ws.scope,
        "user_id": None if shared_group else ws.user_id,
        "user_name": None if shared_group else ws.user_name,
        "chat_kind": ws.chat_kind,
        "chat_id": ws.chat_id,
        "updated_at": time.time(),
    }
    if not any(payload.get(key) for key in ("user_id", "user_name", "chat_id")):
        return
    try:
        tmp = ws.root / (IDENTITY_FILENAME + ".tmp")
        target = ws.root / IDENTITY_FILENAME
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        _LOGGER.warning("persist workspace identity failed | workspace=%s", ws.root)


def read_workspace_identity(root: Path) -> Dict[str, Optional[str]]:
    path = root / IDENTITY_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Optional[str]] = {}
    for key in ("scope", "user_id", "user_name", "chat_kind", "chat_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


__all__ = ["persist_workspace_identity", "read_workspace_identity"]
