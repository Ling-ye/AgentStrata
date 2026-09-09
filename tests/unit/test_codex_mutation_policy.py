from __future__ import annotations

from unittest import mock

from chatcopilot.agent.subagents.delegate_tools import make_delegate_tool
from chatcopilot.agent.subagents.runner import SubagentRuntimeConfig
from chatcopilot.agent.subagents.selector import build_predicate
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS
from chatcopilot.contracts.identity import Role
from chatcopilot.external_tools.dev.file_tools import TOOLS as DEV_FILE_TOOLS
from chatcopilot.external_tools.dev.shell_tools import TOOLS as DEV_SHELL_TOOLS
from chatcopilot.external_tools.mcp_admin.tools import TOOLS as MCP_ADMIN_TOOLS
from chatcopilot.external_tools.web_fetch.tools import web_fetch_page
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.middleware.acp.tool_permissions import (
    build_permission_filter as _make_permission_filter,
)


def _by_name(tools: list[ToolDef]) -> dict[str, ToolDef]:
    return {tool.name: tool for tool in tools}


def test_owner_direct_tools_and_remaining_mutations_have_expected_boundaries() -> None:
    dev = _by_name([*DEV_FILE_TOOLS, *DEV_SHELL_TOOLS])
    admin = _by_name(MCP_ADMIN_TOOLS)

    for name in ("write_file", "run_command"):
        assert dev[name].metadata.get("execution_boundary") is None
        assert dev[name].access == ("owner" if name == "run_command" else "member")
    for name in ("edit_file", "delete_file"):
        assert dev[name].metadata.get("execution_boundary") is None
    assert admin["approve_mcp_server"].metadata.get("execution_boundary") is None
    assert set(admin) == {
        "approve_mcp_server",
        "discover_mcp_server",
        "list_mcp_servers",
        "probe_mcp_server",
    }


def test_owner_direct_tools_are_allowed_only_for_owner() -> None:
    dev = _by_name([*DEV_FILE_TOOLS, *DEV_SHELL_TOOLS])
    owner_filter = _make_permission_filter(Role.OWNER)
    user_filter = _make_permission_filter(Role.USER)

    for name in ("run_command",):
        assert owner_filter(dev[name]) is None
        assert "Owner" in str(user_filter(dev[name]))
    assert owner_filter(web_fetch_page) is None
    assert user_filter(web_fetch_page) is None
    assert user_filter(dev["write_file"]) is None


def test_retired_backend_metadata_does_not_restrict_owner() -> None:
    called = mock.Mock()

    def handler(_args: dict, _context: ToolContext) -> ToolResult:
        called()
        return ToolResult(ok=True, summary="ok")

    tool = ToolDef(
        name="mutate_repository",
        summary="test",
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=handler,
        access="owner",
        metadata={"execution_boundary": "codex"},
    )
    permission_filter = _make_permission_filter(Role.OWNER)
    codex_owner_filter = _make_permission_filter(Role.OWNER)
    codex_user_filter = _make_permission_filter(Role.USER)

    rejection = permission_filter(tool)
    result = ToolExecutor(tools=[tool], permission_filter=permission_filter).execute(
        tool.name,
        {},
    )

    assert rejection is None
    assert result.ok is True
    assert codex_owner_filter(tool) is None
    assert "Owner" in str(codex_user_filter(tool))
    called.assert_called_once()


def test_write_capable_delegates_are_owner_tools_for_every_backend() -> None:
    config = SubagentRuntimeConfig(
        model_env_prefix=None,
        max_model_turns=1,
        max_tool_calls=1,
        timeout_seconds=10,
        max_output_chars=1000,
    )
    for name in ("adapter_forge", "developer"):
        tool = make_delegate_tool(
            "session",
            BUILTIN_SUBAGENTS[name],
            mock.Mock(),
            config,
            lambda _tool: True,
        )
        assert tool.metadata.get("execution_boundary") is None
        if name == "adapter_forge":
            assert tool.access == "owner"


def test_adapter_forge_can_dispatch_code_task_but_cannot_write_directly() -> None:
    allows = build_predicate(BUILTIN_SUBAGENTS["adapter_forge"].selector)
    tools = {
        tool.name: tool
        for tool in [
            *DEV_FILE_TOOLS,
            *DEV_SHELL_TOOLS,
            ToolDef(
                name="start_code_task",
                summary="test",
                input_schema=object_schema(),
                output_schema=object_schema(),
                handler=lambda _args, _context: ToolResult(ok=True, summary="queued"),
                category="development.task.write",
            ),
        ]
    }

    assert allows(tools["start_code_task"]) is True
    assert allows(tools["read_file"]) is True
    assert allows(tools["list_directory"]) is True
    assert allows(tools["search_content"]) is True
    assert allows(tools["write_file"]) is False
    assert allows(tools["edit_file"]) is False
    assert allows(tools["delete_file"]) is False
    assert allows(tools["run_command"]) is False
