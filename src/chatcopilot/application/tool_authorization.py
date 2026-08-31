"""Adapt pure host authorization decisions to Agent capability hooks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from chatcopilot.agent.session import ToolPayloadFilter
from chatcopilot.agent.tools.executor import PermissionFilter
from chatcopilot.authorization.payloads import sanitize_tool_payload
from chatcopilot.authorization.tools import ToolAuthorizationPolicy
from chatcopilot.contracts.authorization import (
    AuthorizationDecision,
    AuthorizationOperation,
    AuthorizationRequest,
    Principal,
    stable_payload_digest,
)
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.contracts.workspace import WorkspaceView


DecisionSink = Callable[[AuthorizationDecision], None]

_DENIAL_MESSAGES = {
    "group-task-diagnostics-denied": "当前群不向成员暴露单轮任务诊断。",
    "group-owner-job-denied": "当前群成员不能访问 Owner 后台任务。",
    "group-background-tool-denied": "当前群成员不能启动后台工具任务。",
    "group-memory-clear-denied": "只有 Owner 可以清空当前群的整份长期记忆。",
    "project-access-denied": "当前角色不能访问项目、主机、配置或内部资料。",
    "codex-route-required": "该持久化变更只能通过配置的 Codex 路由执行。",
    "required-role-not-met": "当前角色不满足工具要求。",
    "private-chat-required": "该工具只允许在私聊中执行。",
}


def build_tool_permission_filter(
    principal: Principal,
    *,
    policy_version: str,
    agent_backend: str,
    owner_only_project_access: bool = False,
    on_decision: DecisionSink | None = None,
) -> PermissionFilter:
    """Bind one trusted Principal to schema projection and executor rechecks."""

    policy = ToolAuthorizationPolicy(
        policy_version=policy_version,
        owner_only_project_access=owner_only_project_access,
    )

    def check(tool: ToolDef) -> str | None:
        tool_name = str(getattr(tool, "name", "") or "")
        request = AuthorizationRequest(
            request_id="tool_"
            + stable_payload_digest(
                {
                    "actor_ref": principal.actor_ref,
                    "policy_version": policy_version,
                    "tool": tool_name,
                }
            )[7:31],
            principal=principal,
            operation=AuthorizationOperation.TOOL,
            target=tool_name,
            params_digest=stable_payload_digest({"phase": "visibility-and-execution"}),
        )
        decision = policy.decide(
            request,
            tool=tool,
            agent_backend=agent_backend,
        )
        if on_decision is not None:
            on_decision(decision)
        if decision.allowed:
            return None
        return _DENIAL_MESSAGES.get(
            decision.code,
            f"工具被宿主权限策略拒绝（{decision.code}）。",
        )

    return check


def build_tool_payload_filter(
    principal: Principal,
    *,
    workspace: WorkspaceView | None,
) -> ToolPayloadFilter:
    """Bind the same trusted Principal to the model-facing result projection."""

    def sanitize(payload: dict[str, Any]) -> dict[str, Any]:
        return sanitize_tool_payload(
            payload,
            role=principal.role,
            workspace=workspace,
        )

    return sanitize


__all__ = [
    "DecisionSink",
    "build_tool_payload_filter",
    "build_tool_permission_filter",
]
