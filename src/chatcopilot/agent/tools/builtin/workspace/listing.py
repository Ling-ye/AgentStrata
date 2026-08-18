"""Workspace inventory listing handler."""
from __future__ import annotations

from typing import Any, Dict, List

from chatcopilot.agent.tools.workspace_context import describe_workspace, resolve_workspace
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.external_tools.shared.tool_spec import HandlerResult
from chatcopilot.agent.tools.builtin.workspace.common import _silent_cleanup

_VALID_SUBDIRS = ("downloads", "results", "uploads", "attachments", "jobs", "tasks")
_GROUP_RESERVED_SUBDIRS = frozenset({"jobs", "tasks", "transcripts"})


def _handler_list_workspace(args: Dict[str, Any]) -> HandlerResult:
    ws = resolve_workspace(create=True)
    subdir = (args.get("subdir") or "").strip()
    limit = int(args.get("limit") or 50)
    recursive = bool(args.get("recursive", False))
    if limit <= 0:
        raise ValueError("limit 必须为正整数")

    if subdir:
        if subdir not in _VALID_SUBDIRS:
            raise ValueError("subdir 只能是 " + " / ".join(_VALID_SUBDIRS) + " / 留空")
        if ws.scope == WORKSPACE_SCOPE_GROUP_SHARED and subdir in _GROUP_RESERVED_SUBDIRS:
            raise PermissionError("群共享空间不开放 jobs/tasks/transcripts 诊断目录")
        targets = [ws.resolve_subdir(subdir)]
    else:
        targets = [ws.root]

    existing_targets = [target for target in targets if target.is_dir()]
    if not existing_targets:
        _silent_cleanup(ws)
        return (
            f"目录不存在: {subdir or ws.relpath(targets[0])}",
            [],
            None,
        )

    entries = []
    for target in existing_targets:
        iterator = target.rglob("*") if recursive else target.iterdir()
        for entry in iterator:
            if ws.scope == WORKSPACE_SCOPE_GROUP_SHARED:
                rel_parts = entry.relative_to(ws.root).parts
                if rel_parts and rel_parts[0] in _GROUP_RESERVED_SUBDIRS:
                    continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, entry, stat.st_size))
    entries.sort(key=lambda x: x[0], reverse=True)
    truncated = len(entries) > limit
    entries = entries[:limit]

    lines: List[str] = []
    for _mtime, path, size in entries:
        kind = "DIR " if path.is_dir() else "FILE"
        rel = ws.relpath(path)
        lines.append(f"{kind} {size:>12} bytes  {rel}")

    scope = "递归" if recursive else "仅顶层"
    summary_lines: List[str] = [
        describe_workspace(ws),
        (
            f"目录: {subdir or ws.relpath(existing_targets[0])} "
            f"({scope}, 共 {len(entries)} 项{'（已截断）' if truncated else ''})"
        ),
        *lines,
    ]
    body = (
        "\n".join(summary_lines)
        if entries
        else f"{describe_workspace(ws)}\n目录为空: {subdir or ws.relpath(existing_targets[0])}"
    )
    _silent_cleanup(ws)
    return (body, [str(target) for target in existing_targets], None)


__all__ = ["_handler_list_workspace"]
