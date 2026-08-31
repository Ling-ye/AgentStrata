"""Pure tool visibility and execution policy owned by the host."""

from __future__ import annotations

from dataclasses import dataclass

from chatcopilot.authorization.policy import make_authorization_decision
from chatcopilot.contracts.authorization import (
    AuthorizationDecision,
    AuthorizationOperation,
    AuthorizationRequest,
)
from chatcopilot.contracts.identity import Role, role_ge
from chatcopilot.contracts.tools import EXECUTION_SYNC, ToolDef


_MEMBER_SAFE_TOOL_CATEGORIES = frozenset(
    {
        "agent.workspace",
        "agent.memory",
        "agent.search",
        "agent.research",
        "career.intelligence",
    }
)


def member_safe_tool(tool: ToolDef) -> bool:
    if tool.requires_role is not None:
        return False
    category = str(tool.category or "").strip().lower()
    if category in _MEMBER_SAFE_TOOL_CATEGORIES:
        return True
    return category == "mcp" and str(tool.metadata.get("mcp_risk") or "").lower() == "search"


def owner_project_access(role: object) -> bool:
    return getattr(role, "value", role) == Role.OWNER.value


@dataclass(frozen=True)
class ToolAuthorizationPolicy:
    """Decide one projected or executed tool call without importing Agent code."""

    policy_version: str
    owner_only_project_access: bool = False

    def decide(
        self,
        request: AuthorizationRequest,
        *,
        tool: ToolDef,
        agent_backend: str = "native",
    ) -> AuthorizationDecision:
        code = self._decision_code(request, tool=tool, agent_backend=agent_backend)
        return make_authorization_decision(
            request,
            allowed=code == "allowed",
            code=code,
            policy_version=self.policy_version,
        )

    def _decision_code(
        self,
        request: AuthorizationRequest,
        *,
        tool: ToolDef,
        agent_backend: str,
    ) -> str:
        if request.operation is not AuthorizationOperation.TOOL:
            return "authorization-operation-mismatch"
        tool_name = str(tool.name or "").strip()
        if not tool_name:
            return "tool-identity-invalid"
        if request.target != tool_name:
            return "tool-target-mismatch"

        principal = request.principal
        chat_kind = principal.conversation.chat_kind.strip().lower()
        is_group = chat_kind == "group"
        has_owner_access = owner_project_access(principal.role)

        if is_group and not has_owner_access:
            if tool_name == "get_task_status":
                return "group-task-diagnostics-denied"
            if tool_name == "get_job_status":
                return "group-owner-job-denied"
            if str(tool.execution_policy or EXECUTION_SYNC) != EXECUTION_SYNC:
                return "group-background-tool-denied"
            if tool_name == "clear_memory":
                return "group-memory-clear-denied"
            if not member_safe_tool(tool):
                return "project-access-denied"

        if self.owner_only_project_access and not has_owner_access and not member_safe_tool(tool):
            return "project-access-denied"

        boundary = str(tool.metadata.get("execution_boundary") or "").strip().lower()
        if boundary == "codex" and agent_backend != "codex":
            return "codex-route-required"

        if tool.requires_role is not None and not role_ge(principal.role, tool.requires_role):
            return "required-role-not-met"

        if bool(tool.metadata.get("private_chat_only")) and chat_kind != "p2p":
            return "private-chat-required"
        return "allowed"


__all__ = [
    "ToolAuthorizationPolicy",
    "member_safe_tool",
    "owner_project_access",
]
