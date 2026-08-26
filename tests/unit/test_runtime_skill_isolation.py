from __future__ import annotations

from pathlib import Path

import pytest

from chatcopilot.agent.runtime import build_agent_runtime
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import (
    TOOL_AUDIENCE_MAIN,
    TOOL_AUDIENCE_SUBAGENT,
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)
from chatcopilot.core.config import ChatConfig


@pytest.fixture(autouse=True)
def _stub_llm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chatcopilot.agent.runtime.LLMClient",
        lambda _config: object(),
    )


def _skill(root: Path, skill_id: str) -> SkillIndexEntry:
    body = root / skill_id / "SKILL.md"
    body.parent.mkdir(parents=True)
    body.write_text(
        f"---\nname: {skill_id}\ndescription: runtime isolation\n---\n\n"
        f"# {skill_id}\n\nbody-{skill_id}\n",
        encoding="utf-8",
    )
    return SkillIndexEntry(
        id=skill_id,
        name=skill_id,
        description="runtime isolation",
        body_path=body,
    )


def test_agent_runtimes_bind_distinct_playbook_indexes(tmp_path: Path) -> None:
    first = _skill(tmp_path, "first")
    second = _skill(tmp_path, "second")
    config = ChatConfig()
    first_runtime = build_agent_runtime(
        chat_config=config,
        tool_packs=("playbooks.reader",),
        skill_index=(first,),
    )
    second_runtime = build_agent_runtime(
        chat_config=config,
        tool_packs=("playbooks.reader",),
        skill_index=(second,),
    )
    first_tool = next(tool for tool in first_runtime.tools if tool.name == "read_bot_skill")
    second_tool = next(tool for tool in second_runtime.tools if tool.name == "read_bot_skill")
    first_result = first_tool.handler({"skill_id": "first"}, ToolContext())
    second_result = second_tool.handler({"skill_id": "second"}, ToolContext())

    assert first_result.data["body"].endswith("body-first\n")
    assert second_result.data["body"].endswith("body-second\n")
    assert first_tool.handler is not second_tool.handler


def test_duplicate_runtime_pack_selection_is_idempotent(tmp_path: Path) -> None:
    skill = _skill(tmp_path, "deduplicated")
    runtime = build_agent_runtime(
        chat_config=ChatConfig(),
        tool_packs=("playbooks.reader", "playbooks.reader"),
        skill_index=(skill,),
    )

    assert runtime.tool_packs == ("playbooks.reader",)
    assert [tool.name for tool in runtime.tools] == ["read_bot_skill"]


def test_runtime_projects_main_and_subagent_tools_in_both_directions() -> None:
    def tool(name: str, audiences: tuple[str, ...]) -> ToolDef:
        def handler(_arguments: dict, _context: ToolContext) -> ToolResult:
            return ToolResult(ok=True, summary="ok", data={})

        return ToolDef(
            name=name,
            summary="Runtime audience projection test tool.",
            input_schema=object_schema({}),
            output_schema=object_schema({}),
            handler=handler,
            category="tests.audience",
            owner="tests",
            module=__name__,
            artifact_kinds=(),
            audiences=audiences,  # type: ignore[arg-type]
        )

    provider = ToolProvider(
        id="tests.audience",
        packs={
            "tests.audience": (
                tool("main_only", (TOOL_AUDIENCE_MAIN,)),
                tool("subagent_only", (TOOL_AUDIENCE_SUBAGENT,)),
                tool("shared", (TOOL_AUDIENCE_MAIN, TOOL_AUDIENCE_SUBAGENT)),
            )
        },
        module=__name__,
    )
    runtime = build_agent_runtime(
        chat_config=ChatConfig(),
        tool_packs=(),
        runtime_providers=(provider,),
    )

    assert [entry["function"]["name"] for entry in runtime.tools_schema] == [
        "main_only",
        "shared",
    ]
    assert tuple(tool.name for tool in runtime.tools) == ("main_only", "shared")
    assert tuple(tool.name for tool in runtime.subagent_tools) == (
        "subagent_only",
        "shared",
    )
