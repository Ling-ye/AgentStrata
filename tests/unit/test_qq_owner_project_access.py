from __future__ import annotations

from pathlib import Path

from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.acp.tool_permissions import (
    build_permission_filter as _make_permission_filter,
)
from chatcopilot.middleware.payload_sanitizer import sanitize_tool_payload_for_role
from chatcopilot.core.workspace_runtime import Workspace


def test_lingye_bot_member_tool_surface_is_explicit_and_fail_closed(
    tmp_path: Path,
) -> None:
    spec = load_botspec("bots/lingye-copilot-qq/bot.yaml")
    tools = discover_tools(tool_packs=spec.tools.packs, exclude_tools=spec.tools.hide)
    workspace = Workspace(
        root=tmp_path / "workspace",
        chat_kind="p2p",
        chat_id=None,
        user_id="user",
    ).ensure()
    member_filter = _make_permission_filter(
        Role.USER,
        workspace,
        owner_only_project_access=spec.access.owner_only_project_access,
        agent_backend=spec.agents.backend,
    )
    owner_filter = _make_permission_filter(
        Role.OWNER,
        workspace,
        owner_only_project_access=spec.access.owner_only_project_access,
        agent_backend=spec.agents.backend,
    )
    owner_group_filter = _make_permission_filter(
        Role.OWNER,
        Workspace(
            root=tmp_path / "group_group" / "shared",
            chat_kind="group",
            chat_id="group",
            user_id="owner",
            scope=WORKSPACE_SCOPE_GROUP_SHARED,
        ).ensure(),
        owner_only_project_access=spec.access.owner_only_project_access,
        agent_backend=spec.agents.backend,
    )
    user_group_filter = _make_permission_filter(
        Role.USER,
        Workspace(
            root=tmp_path / "group_group" / "shared",
            chat_kind="group",
            chat_id="group",
            user_id="user",
            scope=WORKSPACE_SCOPE_GROUP_SHARED,
        ).ensure(),
        owner_only_project_access=spec.access.owner_only_project_access,
        agent_backend=spec.agents.backend,
    )
    member_visible = {tool.name for tool in tools if member_filter(tool) is None}
    owner_visible = {tool.name for tool in tools if owner_filter(tool) is None}
    owner_group_visible = {tool.name for tool in tools if owner_group_filter(tool) is None}
    user_group_visible = {tool.name for tool in tools if user_group_filter(tool) is None}

    assert {
        "list_workspace",
        "read_text_head",
        "read_memory",
        "append_memory",
        "career_intel_query",
    }.issubset(member_visible)
    assert {
        "win_read_file",
        "win_grep",
        "win_glob",
        "read_bot_skill",
        "owner_list_workspaces",
        "owner_read_workspace_file",
        "wiki_search",
        "list_mcp_servers",
        "start_code_task",
        "cancel_code_task",
        "persona_show",
        "persona_set",
        "persona_append",
        "persona_clear",
    }.isdisjoint(member_visible)
    assert {
        "win_read_file",
        "read_bot_skill",
        "wiki_search",
        "list_mcp_servers",
        "start_code_task",
    }.issubset(owner_visible)
    assert {
        "win_read_file",
        "read_bot_skill",
        "list_mcp_servers",
        "start_code_task",
        "owner_list_workspaces",
    }.issubset(owner_group_visible)
    assert "wiki_search" not in owner_group_visible
    assert {
        "read_memory",
        "append_memory",
        "clear_memory",
    }.issubset(owner_group_visible)
    assert {
        "win_read_file",
        "read_bot_skill",
        "wiki_search",
        "list_mcp_servers",
        "start_code_task",
        "owner_list_workspaces",
    }.isdisjoint(user_group_visible)
    assert {
        "list_workspace",
        "read_text_head",
        "read_memory",
        "append_memory",
        "career_intel_query",
    }.issubset(user_group_visible)
    assert {
        "clear_memory",
        "persona_show",
        "persona_set",
        "persona_append",
        "persona_clear",
    }.isdisjoint(user_group_visible)


def test_admin_payload_is_sanitized_like_user(tmp_path: Path) -> None:
    workspace = Workspace(
        root=tmp_path / "workspace",
        chat_kind="p2p",
        chat_id=None,
        user_id="user",
    ).ensure()
    outside = str(tmp_path / "private" / "secret.txt")
    payload = {
        "ok": False,
        "summary": f"workspace={workspace.root} user=user private summary",
        "outputs": [outside],
        "console_tail": "trace with private path",
        "doc_links": ["internal-link"],
        "error": "PermissionError: denied\n/private/trace.py:10",
    }

    owner = sanitize_tool_payload_for_role(payload, Role.OWNER, workspace)
    admin = sanitize_tool_payload_for_role(payload, Role.ADMIN, workspace)
    user = sanitize_tool_payload_for_role(payload, Role.USER, workspace)

    assert owner["console_tail"] == "trace with private path"
    for restricted in (admin, user):
        assert "console_tail" not in restricted
        assert "doc_links" not in restricted
        assert restricted["outputs"] == ["secret.txt"]
        assert restricted["error"] == "PermissionError: denied"
        assert str(workspace.root) not in restricted["summary"]
        assert "user=user" not in restricted["summary"]
