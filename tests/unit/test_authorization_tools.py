from __future__ import annotations

from pathlib import Path

from chatcopilot.authorization.payloads import sanitize_tool_payload
from chatcopilot.authorization.tools import ToolAuthorizationPolicy
from chatcopilot.contracts.authorization import (
    AuthorizationOperation,
    AuthorizationRequest,
    Principal,
    stable_payload_digest,
)
from chatcopilot.contracts.identity import ConversationIdentity, Role
from chatcopilot.contracts.tools import (
    EXECUTION_USER_SERIAL_BACKGROUND,
    ToolDef,
    ToolResult,
    object_schema,
)
from chatcopilot.contracts.workspace import WorkspaceView


def _tool(
    name: str,
    *,
    category: str = "agent.workspace",
    role: str | None = None,
    background: bool = False,
    metadata: dict[str, object] | None = None,
) -> ToolDef:
    return ToolDef(
        name=name,
        summary=name,
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=lambda _args, _context: ToolResult(ok=True),
        category=category,
        requires_role=role,
        execution_policy=(EXECUTION_USER_SERIAL_BACKGROUND if background else "sync"),
        metadata=dict(metadata or {}),
    )


def _request(
    *,
    role: Role,
    target: str,
    chat_kind: str = "group",
    operation: AuthorizationOperation = AuthorizationOperation.TOOL,
) -> AuthorizationRequest:
    conversation = ConversationIdentity(platform="qq", chat_kind=chat_kind, chat_id="12345")
    principal = Principal(
        channel="qq-personal",
        account_id="54321",
        conversation=conversation,
        user_id="67890",
        role=role,
        evidence_digest="sha256:" + "a" * 64,
    )
    return AuthorizationRequest(
        request_id="req-1",
        principal=principal,
        operation=operation,
        target=target,
        params_digest=stable_payload_digest({}),
    )


def test_group_member_receives_only_sync_member_safe_tools() -> None:
    policy = ToolAuthorizationPolicy(policy_version="v1")
    allowed = policy.decide(
        _request(role=Role.USER, target="search_public"),
        tool=_tool("search_public", category="agent.search"),
    )
    background = policy.decide(
        _request(role=Role.USER, target="search_later"),
        tool=_tool("search_later", category="agent.search", background=True),
    )
    project = policy.decide(
        _request(role=Role.ADMIN, target="project_status"),
        tool=_tool("project_status", category="project." + "internal"),
    )

    assert allowed.allowed is True
    assert background.code == "group-background-tool-denied"
    assert project.code == "project-access-denied"


def test_group_owner_keeps_role_but_private_and_codex_rules_still_apply() -> None:
    policy = ToolAuthorizationPolicy(policy_version="v1")
    private = policy.decide(
        _request(role=Role.OWNER, target="private_control"),
        tool=_tool("private_control", metadata={"private_chat_only": True}),
    )
    codex = policy.decide(
        _request(role=Role.OWNER, target="mutate_source"),
        tool=_tool("mutate_source", metadata={"execution_boundary": "codex"}),
        agent_backend="native",
    )

    assert private.code == "private-chat-required"
    assert codex.code == "codex-route-required"


def test_tool_decision_rejects_operation_and_target_drift() -> None:
    policy = ToolAuthorizationPolicy(policy_version="v1")
    tool = _tool("search_public")

    wrong_operation = policy.decide(
        _request(
            role=Role.USER,
            target=tool.name,
            operation=AuthorizationOperation.RESOURCE,
        ),
        tool=tool,
    )
    wrong_target = policy.decide(
        _request(role=Role.USER, target="other"),
        tool=tool,
    )

    assert wrong_operation.code == "authorization-operation-mismatch"
    assert wrong_target.code == "tool-target-mismatch"


def test_non_owner_payload_removes_host_details_and_keeps_relative_outputs(tmp_path: Path) -> None:
    workspace = WorkspaceView(
        root=tmp_path,
        chat_kind="group",
        chat_id="12345",
        user_id=None,
        scope="group_shared",
    )
    inside = tmp_path / "results" / "report.txt"
    outside = Path("/srv/private/config.txt")
    payload = {
        "ok": False,
        "summary": f"workspace={tmp_path} user=67890",
        "outputs": [str(inside), str(outside)],
        "error": f"failed at {tmp_path}\ntraceback",
        "console_tail": "secret",
        "details": {"path": str(outside)},
    }

    result = sanitize_tool_payload(payload, role=Role.USER, workspace=workspace)

    assert result["outputs"] == ["results/report.txt", "config.txt"]
    assert result["summary"] == "private-context private-context"
    assert result["error"] == "failed at private-space"
    assert "console_tail" not in result
    assert "details" not in result


def test_owner_payload_is_copied_without_redaction() -> None:
    payload = {"ok": True, "console_tail": "visible"}
    result = sanitize_tool_payload(payload, role=Role.OWNER)

    assert result == payload
    assert result is not payload
