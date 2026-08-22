"""Owner-only cross-workspace inspection handlers."""
from __future__ import annotations

from typing import Any, Dict

from chatcopilot.agent.tools.workspace_context import (
    list_workspace_inventories,
    resolve_workspace,
    resolve_workspace_root,
)
from chatcopilot.contracts.tools import HandlerResult
from chatcopilot.agent.tools.builtin.workspace.common import _format_bytes, _format_mtime, _require

def _handler_owner_list_workspaces(args: Dict[str, Any]) -> HandlerResult:
    limit = int(args.get("limit") or 50)
    if limit <= 0 or limit > 500:
        raise ValueError("limit 必须在 [1, 500] 区间内")

    root = resolve_workspace_root(resolve_workspace(create=False))
    inventories = list_workspace_inventories(root)
    shown = inventories[:limit]
    known_names = _known_identity_names()

    unique_users = {item.user_id for item in inventories if item.user_id}
    named_users = {
        item.user_id
        for item in inventories
        if item.user_id and (item.user_name or known_names.get(item.user_id))
    }
    total_files = sum(item.total_files for item in inventories)
    total_bytes = sum(item.total_bytes for item in inventories)
    lines = [
        f"workspace_root={root}",
        (
            f"已识别工作区 {len(inventories)} 个；"
            f"含明确 user_id 的用户 {len(unique_users)} 个；"
            f"含飞书姓名的用户 {len(named_users)} 个；"
            f"总文件 {total_files} 个；总大小 {_format_bytes(total_bytes)}。"
        ),
    ]
    if len(inventories) > limit:
        lines.append(f"仅展示最近更新的前 {limit} 个。")

    for item in shown:
        display_name = item.user_name or known_names.get(item.user_id or "") or "-"
        category_bits = []
        for name in ("attachments", "downloads", "results", "uploads", "transcripts", "jobs", "memory", "identity"):
            stat = item.stats.get(name) or {}
            if stat.get("exists") or stat.get("files"):
                category_bits.append(
                    f"{name}:{stat.get('files', 0)} files/{_format_bytes(int(stat.get('bytes') or 0))}"
                )
        lines.append(
            "- "
            f"workspace={item.relative_path} "
            f"layout={item.layout} "
            f"chat={item.chat_kind or '-'}:{item.chat_id or '-'} "
            f"user={item.user_id or '-'} "
            f"name={display_name} "
            f"total={item.total_files} files/{_format_bytes(item.total_bytes)} "
            f"updated={_format_mtime(item.latest_mtime)} "
            f"data=[{'; '.join(category_bits) if category_bits else 'empty'}]"
        )

    return ("\n".join(lines), [str(root)], None)


def _known_identity_names() -> Dict[str, str]:
    """从 Owner/Admin 静态配置补充 user_id -> name。"""
    try:
        from chatcopilot.core.access import get_admins, get_owners
    except Exception:  # noqa: BLE001
        return {}

    names: Dict[str, str] = {}
    for identity in [*get_admins(), *get_owners()]:
        user_id = (identity.user_id or "").strip()
        name = (identity.name or "").strip()
        if user_id and name:
            names[user_id] = name
    return names


def _handler_owner_read_workspace_file(args: Dict[str, Any]) -> HandlerResult:
    workspace_path = _require(args, "workspace_path").strip()
    file_path = _require(args, "file_path").strip()
    kb = int(args.get("kb") or 8)
    if kb <= 0 or kb > 512:
        raise ValueError("kb 必须在 (0, 512] 区间内")

    root = resolve_workspace_root(resolve_workspace(create=False))
    workspace = (root / workspace_path).resolve()
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise PermissionError("workspace_path 越出工作区总根目录") from exc
    if not workspace.is_dir():
        raise FileNotFoundError(f"工作区不存在: {workspace_path}")

    target = (workspace / file_path).resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError("file_path 越出指定工作区") from exc
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在: {workspace_path}/{file_path}")

    size_limit = kb * 1024
    with target.open("rb") as fp:
        raw = fp.read(size_limit)
    if b"\x00" in raw:
        raise ValueError(f"疑似二进制文件，拒绝读取: {workspace_path}/{file_path}")

    text = raw.decode("utf-8", errors="replace")
    truncated_hint = "（已截断）" if target.stat().st_size > size_limit else ""
    rel_target = target.relative_to(root)
    return (
        f"读取 {rel_target} 前 {kb}KB{truncated_hint}\n----\n{text}",
        [str(target)],
        None,
    )


__all__ = ["_handler_owner_list_workspaces", "_handler_owner_read_workspace_file"]
