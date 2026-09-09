from __future__ import annotations

from pathlib import Path

from chatcopilot.application.tool_authorization import (
    build_tool_payload_filter,
    build_tool_permission_filter,
)
from chatcopilot.contracts.authorization import Principal, stable_payload_digest
from chatcopilot.contracts.identity import ConversationIdentity, Role
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.contracts.workspace import WorkspaceView


def _principal(*, role: Role, kind: str = "group") -> Principal:
    return Principal(
        channel="qq",
        account_id="10001",
        conversation=ConversationIdentity("qq", kind, "20002"),
        user_id="30003",
        role=role,
        evidence_digest=stable_payload_digest({"event": "one"}),
    )


def _tool(
    name: str,
    *,
    category: str,
    access: str = "owner",
    private: bool = False,
) -> ToolDef:
    return ToolDef(
        name=name,
        summary=name,
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=lambda _args, _context: ToolResult(ok=True),
        category=category,
        access=access,
        metadata={"private_chat_only": private},
    )


def test_permission_filter_reuses_pure_policy_for_projection_and_execution() -> None:
    observed = []
    permission = build_tool_permission_filter(
        _principal(role=Role.USER),
        policy_version="policy-v1",
        on_decision=observed.append,
    )
    public = _tool("search_public", category="agent.search", access="member")
    internal = _tool("project_status", category="project." + "internal")

    assert permission(public) is None
    assert permission(internal) == "该操作仅限 Owner；成员仅可使用公共查询和当前会话基础能力。"
    assert [decision.allowed for decision in observed] == [True, False]
    assert observed[0].actor_ref == observed[1].actor_ref
    assert observed[1].code == "owner-required"


def test_owner_can_use_tools_in_groups() -> None:
    permission = build_tool_permission_filter(
        _principal(role=Role.OWNER),
        policy_version="policy-v1",
    )

    assert permission(_tool("owner_private", category="runtime", private=True)) is None


def test_payload_filter_uses_principal_role_and_workspace(tmp_path: Path) -> None:
    workspace = WorkspaceView(
        root=tmp_path,
        chat_kind="group",
        chat_id="20002",
        scope="group_shared",
    )
    payload_filter = build_tool_payload_filter(
        _principal(role=Role.USER),
        workspace=workspace,
    )

    result = payload_filter(
        {
            "ok": True,
            "outputs": [str(tmp_path / "results" / "answer.txt")],
            "console_tail": "host detail",
        }
    )

    assert result == {"ok": True, "outputs": ["results/answer.txt"]}
