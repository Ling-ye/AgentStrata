"""One business authorization policy for every Agent runtime entry."""

from __future__ import annotations

from dataclasses import dataclass
from chatcopilot.authorization.policy import make_authorization_decision
from chatcopilot.contracts.authorization import (
    AuthorizationDecision,
    AuthorizationOperation,
    AuthorizationRequest,
)
from chatcopilot.contracts.tools import ToolDef, tool_access_allowed


def member_safe_tool(tool: ToolDef) -> bool:
    return tool.access == "member"


def owner_project_access(role: object) -> bool:
    return getattr(role, "value", role) == "owner"


@dataclass(frozen=True)
class ToolAuthorizationPolicy:
    policy_version: str

    def decide(self, request: AuthorizationRequest, *, tool: ToolDef) -> AuthorizationDecision:
        if request.operation is not AuthorizationOperation.TOOL:
            code = "authorization-operation-mismatch"
        elif not tool.name or request.target != tool.name:
            code = "tool-target-mismatch"
        else:
            code = (
                "allowed"
                if tool_access_allowed(request.principal.role, tool.access)
                else "owner-required"
            )
        return make_authorization_decision(
            request, allowed=code == "allowed", code=code, policy_version=self.policy_version
        )


__all__ = ["ToolAuthorizationPolicy", "member_safe_tool", "owner_project_access"]
