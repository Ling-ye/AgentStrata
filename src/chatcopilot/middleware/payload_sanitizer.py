"""按角色裁剪工具响应里可能泄漏内部细节的字段。

agent 在执行完一个工具后会把 ``ToolResult.to_llm_payload()`` 回灌给 LLM；
middleware 通过 ``make_payload_sanitizer(role, workspace)`` 拿到一个绑好上下文
的 callable，作为 ``AgentRuntime`` 的 ``tool_payload_filter`` 注入。

普通用户看到的 tool result 里要剥掉的"内部字段"：
- ``console_tail``: 工具捕获的 stdout/stderr 尾段，可能含绝对路径 / Python 堆栈
- ``doc_links``:    markdown 文档链接，会让 LLM 误以为可以贴文档给用户
- ``error`` 详情:   失败时返回的 traceback，会泄漏内部文件路径与行号

agent 完全不感知 Role；本模块是策略的唯一落点。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from chatcopilot.middleware.access_control import Role
from chatcopilot.core.workspace_runtime import Workspace

_USER_STRIP_FIELDS = ("console_tail", "doc_links", "details", "stage")


PayloadSanitizer = Callable[[Dict[str, Any]], Dict[str, Any]]


def make_payload_sanitizer(role: Role, workspace: Optional[Workspace]) -> PayloadSanitizer:
    """绑定当前会话角色与 workspace，返回 agent 注入用的 filter 闭包。"""

    def _sanitize(payload: Dict[str, Any]) -> Dict[str, Any]:
        return sanitize_tool_payload_for_role(payload, role, workspace)

    return _sanitize


def sanitize_tool_payload_for_role(
    payload: Dict[str, Any],
    role: Role,
    workspace: Optional[Workspace] = None,
) -> Dict[str, Any]:
    """按角色裁剪 ``ToolResult.to_llm_payload()`` 的输出。

    - OWNER: 原样返回（payload 拷贝）。
    - ADMIN / USER: 抹掉绝对路径（outputs 退化为相对路径或 basename），删除可能泄漏
      内部细节的字段，error 截短到第一行（去掉 traceback）。
    """
    if not isinstance(payload, dict):
        return payload

    out = dict(payload)
    if role == Role.OWNER:
        return out

    for key in _USER_STRIP_FIELDS:
        out.pop(key, None)

    outputs = out.get("outputs")
    if isinstance(outputs, list):
        out["outputs"] = [
            _workspace_relative_or_basename(p, workspace) if isinstance(p, str) else p
            for p in outputs
        ]

    summary = out.get("summary")
    if isinstance(summary, str) and summary:
        out["summary"] = _redact_workspace_context(summary, workspace)

    err = out.get("error")
    if isinstance(err, str) and err:
        first_line = err.splitlines()[0] if err.splitlines() else err
        out["error"] = _redact_workspace_context(first_line.strip(), workspace)

    return out


def _basename_only(path: str) -> str:
    if not isinstance(path, str) or not path:
        return path
    last_slash = max(path.rfind("/"), path.rfind("\\"))
    return path[last_slash + 1:] if last_slash >= 0 else path


def _workspace_relative_or_basename(path: str, workspace: Optional[Workspace]) -> str:
    if not isinstance(path, str) or not path:
        return path
    if workspace is None:
        return _basename_only(path)
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            return str(candidate).replace("\\", "/")
        rel = candidate.resolve().relative_to(workspace.root.resolve())
        return str(rel).replace("\\", "/")
    except (OSError, ValueError):
        return _basename_only(path)


def _redact_workspace_context(text: str, workspace: Optional[Workspace]) -> str:
    redacted = text
    if workspace is not None:
        redacted = redacted.replace(str(workspace.root), "private-space")
    redacted = re.sub(
        r"(?<!\w)(?:workspace|chat|user|name)=[^\s\r\n]+",
        "private-context",
        redacted,
    )
    return redacted


__all__ = ["PayloadSanitizer", "make_payload_sanitizer", "sanitize_tool_payload_for_role"]
