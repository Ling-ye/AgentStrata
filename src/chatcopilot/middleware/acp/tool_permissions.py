"""Role, channel, and shared-group tool projection policy."""

from __future__ import annotations

from typing import Any

from chatcopilot.agent.tools.executor import PermissionFilter
from chatcopilot.contracts import Role, role_ge, role_value
from chatcopilot.contracts.tools import EXECUTION_SYNC
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED, WorkspaceView
from chatcopilot.core.workspace_runtime import normalize_chat_kind

_MEMBER_SAFE_TOOL_CATEGORIES = frozenset(
    {
        "agent.workspace",
        "agent.memory",
        "agent.search",
        "agent.research",
        "career.intelligence",
    }
)
_MEMBER_PROJECT_ACCESS_DENIED = (
    "当前角色仅可使用公开信息查询和当前会话空间能力（QQ 群内为当前群共享空间）；"
    "项目、主机、机器人配置、内部资料及管理能力仅限 Owner。"
)


def build_permission_filter(
    role: Any,
    workspace: WorkspaceView | None = None,
    *,
    agent_backend: str = "native",
    owner_only_project_access: bool = False,
) -> PermissionFilter:
    """Build the final fail-closed tool visibility and execution policy."""

    def check(tool: Any) -> str | None:
        shared_group = workspace is not None and workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
        tool_name = str(getattr(tool, "name", "") or "")
        if shared_group and tool_name == "get_task_status":
            return (
                "QQ 群共享会话不保存成员可见的单轮 task 诊断；"
                "请使用当前回复或 Owner 后台 job 状态。"
            )
        group_member = shared_group and not owner_project_access(role)
        if group_member:
            if tool_name == "get_job_status":
                return "QQ 群普通成员不能查询 Owner 后台 job；这些控制面记录不属于群共享文件。"
            if str(getattr(tool, "execution_policy", EXECUTION_SYNC)) != EXECUTION_SYNC:
                return "QQ 群共享会话不启动后台任务；请改用同步的当前群工作区能力。"
            if tool_name == "clear_memory":
                return "只有 Owner 可以清空当前群的整份长期记忆。"
            if not member_safe_tool(tool):
                return _MEMBER_PROJECT_ACCESS_DENIED
        if (
            owner_only_project_access
            and not member_safe_tool(tool)
            and not owner_project_access(role)
        ):
            return _MEMBER_PROJECT_ACCESS_DENIED
        if (
            str(getattr(tool, "metadata", {}).get("execution_boundary") or "") == "codex"
            and agent_backend != "codex"
        ):
            return (
                f"工具 {tool.name} 属于持久化变更，只能通过 Codex code route 执行；"
                "普通 Agent 无权调用。"
            )
        required = getattr(tool, "requires_role", None)
        if required is not None and not role_ge(role, required):
            return (
                f"工具 {tool.name} 需要 {role_value(required)} 及以上权限；"
                f"当前用户角色 {role_value(role)}，拒绝执行。"
            )
        if bool(getattr(tool, "metadata", {}).get("private_chat_only")):
            kind = normalize_chat_kind(
                getattr(workspace, "chat_kind", None),
                getattr(workspace, "chat_id", None),
            )
            if kind != "p2p":
                return f"工具 {tool.name} 仅允许在私聊中执行。"
        return None

    return check


def member_safe_tool(tool: Any) -> bool:
    if getattr(tool, "requires_role", None) is not None:
        return False
    category = str(getattr(tool, "category", "") or "").strip().lower()
    if category in _MEMBER_SAFE_TOOL_CATEGORIES:
        return True
    metadata = getattr(tool, "metadata", {}) or {}
    return category == "mcp" and str(metadata.get("mcp_risk") or "").lower() == "search"


def owner_project_access(role: Any) -> bool:
    return role_value(role) == Role.OWNER.value


__all__ = ["build_permission_filter", "member_safe_tool", "owner_project_access"]
