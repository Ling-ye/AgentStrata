from __future__ import annotations

from types import SimpleNamespace

from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.contracts import Role, role_ge, role_value
from chatcopilot.external_tools.shared.tool_spec import ToolDef, build_openai_schema


def _tool(name: str, *, requires_role: str | None = None) -> ToolDef:
    return ToolDef(
        name=name,
        summary=name,
        properties={},
        required=[],
        handler=lambda _args: ("ok", [], None),
        requires_role=requires_role,
    )


def _permission_filter(role: Role):
    def _filter(tool: ToolDef) -> str | None:
        if tool.requires_role is None or role_ge(role, tool.requires_role):
            return None
        return (
            f"工具 {tool.name} 需要 {role_value(tool.requires_role)} 及以上权限；"
            f"当前用户角色 {role_value(role)}，拒绝执行。"
        )

    return _filter


def _runtime(*tools: ToolDef) -> AgentRuntime:
    return AgentRuntime(
        llm=object(),
        tools=tuple(tools),
        tools_schema=tuple(build_openai_schema(tool) for tool in tools),
        runtime_config=SimpleNamespace(runtime=SimpleNamespace(max_tool_retries=1)),
    )


def test_user_session_cannot_see_or_call_owner_only_tool() -> None:
    runtime = _runtime(_tool("normal_tool"), _tool("owner_tool", requires_role="owner"))
    session = runtime.new_session(
        session_id="s1",
        system_baseline="baseline",
        permission_filter=_permission_filter(Role.USER),
    )

    schema_names = {entry["function"]["name"] for entry in session.tools_schema}
    assert schema_names == {"normal_tool"}

    result = session.executor.execute("owner_tool", {})
    assert result.ok is False
    assert "需要 owner" in (result.error or "")


def test_owner_session_can_see_and_call_owner_only_tool() -> None:
    runtime = _runtime(_tool("normal_tool"), _tool("owner_tool", requires_role="owner"))
    session = runtime.new_session(
        session_id="s1",
        system_baseline="baseline",
        permission_filter=_permission_filter(Role.OWNER),
    )

    schema_names = {entry["function"]["name"] for entry in session.tools_schema}
    assert schema_names == {"normal_tool", "owner_tool"}

    result = session.executor.execute("owner_tool", {})
    assert result.ok is True


def test_runtime_passes_retriever_without_changing_tool_schema() -> None:
    retriever = object()
    runtime = _runtime(_tool("normal_tool"))
    runtime.retriever = retriever

    session = runtime.new_session(session_id="s1", system_baseline="baseline")

    schema_names = {entry["function"]["name"] for entry in session.tools_schema}
    assert schema_names == {"normal_tool"}
    assert session.retriever is retriever
