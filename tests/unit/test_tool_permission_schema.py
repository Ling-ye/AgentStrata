from __future__ import annotations

from tests.prompt_plan_fixture import prompt_input

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.context.prompt_plan import render_native_prefix
from chatcopilot.contracts import Role, role_ge, role_value
from chatcopilot.contracts.skills import SkillIndexEntry
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


def _native(session):
    return session.backend.native_session(session.backend_session_ref)


def test_user_session_cannot_see_or_call_owner_only_tool() -> None:
    runtime = _runtime(_tool("normal_tool"), _tool("owner_tool", requires_role="owner"))
    session = runtime.new_session(
        session_id="s1",
        prompt_input=prompt_input("baseline"),
        permission_filter=_permission_filter(Role.USER),
    )

    concrete = _native(session)
    schema_names = {entry["function"]["name"] for entry in concrete.tools_schema}
    assert schema_names == {"normal_tool"}

    result = session.tool_executor.execute("owner_tool", {})
    assert result.ok is False
    assert "需要 owner" in (result.error or "")


def test_owner_session_can_see_and_call_owner_only_tool() -> None:
    runtime = _runtime(_tool("normal_tool"), _tool("owner_tool", requires_role="owner"))
    session = runtime.new_session(
        session_id="s1",
        prompt_input=prompt_input("baseline"),
        permission_filter=_permission_filter(Role.OWNER),
    )

    concrete = _native(session)
    schema_names = {entry["function"]["name"] for entry in concrete.tools_schema}
    assert schema_names == {"normal_tool", "owner_tool"}

    result = session.tool_executor.execute("owner_tool", {})
    assert result.ok is True


def test_runtime_passes_retriever_without_changing_tool_schema() -> None:
    retriever = object()
    runtime = _runtime(_tool("normal_tool"))
    runtime.retriever = retriever

    session = runtime.new_session(session_id="s1", prompt_input=prompt_input("baseline"))

    concrete = _native(session)
    schema_names = {entry["function"]["name"] for entry in concrete.tools_schema}
    assert schema_names == {"normal_tool"}
    assert concrete.retriever is retriever


def test_session_can_explicitly_hide_bot_skill_index() -> None:
    runtime = _runtime(_tool("normal_tool"))
    runtime.skill_index = (
        SkillIndexEntry(
            id="internal-playbook",
            name="Internal Playbook",
            description="internal",
            body_path=Path("/tmp/internal-playbook.md"),
        ),
    )

    owner_input = replace(prompt_input("baseline"), skill_index=runtime.skill_index)
    owner = runtime.new_session(session_id="owner", prompt_input=owner_input)
    member = runtime.new_session(
        session_id="member",
        prompt_input=prompt_input("baseline"),
    )

    owner_text = render_native_prefix(_native(owner).prompt_plan)[0]["content"]
    member_text = render_native_prefix(_native(member).prompt_plan)[0]["content"]
    assert "internal-playbook" in owner_text
    assert "internal-playbook" not in member_text
