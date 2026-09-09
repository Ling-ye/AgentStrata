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
from chatcopilot.core.observation_context import current_permission_phase, observe
from chatcopilot.agent.trace import current_trace
from chatcopilot.contracts.agent import ToolAuthorizationChecked
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.contracts.workspace import WorkspaceView


DecisionSink = Callable[[AuthorizationDecision], None]

_DENIAL_MESSAGES = {"owner-required": "该操作仅限 Owner；成员仅可使用公共查询和当前会话基础能力。"}


def build_tool_permission_filter(
    principal: Principal,
    *,
    policy_version: str,
    on_decision: DecisionSink | None = None,
) -> PermissionFilter:
    """Bind one trusted Principal to schema projection and executor rechecks."""

    policy = ToolAuthorizationPolicy(
        policy_version=policy_version,
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
        )
        trace = current_trace()
        evidence = dict(name=tool_name, phase=current_permission_phase(), allowed=decision.allowed,
                        code=decision.code, policy_version=policy_version, role=principal.role.value,
                        trace_id=trace.trace_id if trace else None, span_id=trace.span_id if trace else None)
        if trace and trace.sink:
            try:
                trace.sink(ToolAuthorizationChecked(**evidence))
            except Exception:
                pass
        else:
            observe("tool_authorization", **evidence)
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
    public_output: bool = False,
) -> ToolPayloadFilter:
    """Bind the same trusted Principal to the model-facing result projection."""

    def sanitize(payload: dict[str, Any]) -> dict[str, Any]:
        return sanitize_tool_payload(
            payload,
            role=principal.role,
            workspace=workspace,
            public_output=public_output,
        )

    return sanitize


__all__ = [
    "DecisionSink",
    "build_tool_payload_filter",
    "build_tool_permission_filter",
]
