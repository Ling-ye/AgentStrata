from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.application.agent_runtime import (
    AgentRuntimeAssemblyProfile,
    AgentRuntimeOverrides,
    assemble_agent_runtime,
    materialize_agent_runtime,
    project_agent_runtime,
)
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.core.config import ChatConfig


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(
            llm=SimpleNamespace(
                research_model="research-model",
                research_env_prefix=None,
            )
        ),
        tool_packs=("workspace.read_write", "persona.control", "memory.chat"),
        exclude_tools=("hidden_tool",),
        skills=("skill-a",),
        rag_sources=("rag-a",),
        mcp_servers=("mcp-a",),
        subagents=SubagentSpec(include=("developer",)),
        agent_backend="codex",
    )


def test_interactive_projection_preserves_selected_bot_runtime() -> None:
    chat_config = ChatConfig()

    projection = project_agent_runtime(_runtime(), chat_config=chat_config)

    assert projection.chat_config is chat_config
    assert projection.research_llm_config.model == "research-model"
    assert projection.tool_packs == (
        "workspace.read_write",
        "persona.control",
        "memory.chat",
    )
    assert projection.exclude_tools == ("hidden_tool",)
    assert projection.skill_index == ("skill-a",)
    assert projection.rag_sources == ("rag-a",)
    assert projection.mcp_servers == ("mcp-a",)
    assert projection.subagents.include == ("developer",)
    assert projection.agent_backend == "codex"
    assert projection.assembly_profile == "interactive"


def test_detached_profile_and_overrides_are_explicit() -> None:
    replacement_subagents = SubagentSpec()

    projection = project_agent_runtime(
        _runtime(),
        chat_config=ChatConfig(),
        profile=AgentRuntimeAssemblyProfile.DETACHED,
        overrides=AgentRuntimeOverrides(
            rag_sources=(),
            mcp_servers=(),
            subagents=replacement_subagents,
            agent_backend="native",
        ),
    )

    assert projection.tool_packs == ("workspace.read_write", "memory.chat")
    assert projection.rag_sources == ()
    assert projection.mcp_servers == ()
    assert projection.subagents is replacement_subagents
    assert projection.agent_backend == "native"
    assert projection.assembly_profile == "detached"


def test_detached_override_cannot_reintroduce_interactive_only_pack() -> None:
    projection = project_agent_runtime(
        _runtime(),
        chat_config=ChatConfig(),
        profile=AgentRuntimeAssemblyProfile.DETACHED,
        overrides=AgentRuntimeOverrides(
            tool_packs=("persona.control", "memory.chat"),
        ),
    )

    assert projection.tool_packs == ("memory.chat",)


def test_explicit_empty_tool_pack_override_wins_over_profile() -> None:
    projection = project_agent_runtime(
        _runtime(),
        chat_config=ChatConfig(),
        profile=AgentRuntimeAssemblyProfile.DETACHED,
        overrides=AgentRuntimeOverrides(tool_packs=()),
    )

    assert projection.tool_packs == ()


def test_materialization_forwards_the_complete_projection(monkeypatch) -> None:
    projection = project_agent_runtime(_runtime(), chat_config=ChatConfig())
    expected = object()
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(
        "chatcopilot.application.agent_runtime.build_agent_runtime",
        build,
    )

    assert materialize_agent_runtime(projection) is expected
    assert captured == {
        "chat_config": projection.chat_config,
        "research_llm_config": projection.research_llm_config,
        "tool_packs": projection.tool_packs,
        "exclude_tools": projection.exclude_tools,
        "runtime_providers": projection.runtime_providers,
        "skill_index": projection.skill_index,
        "rag_sources": projection.rag_sources,
        "mcp_servers": projection.mcp_servers,
        "subagents": projection.subagents,
        "agent_backend": projection.agent_backend,
        "assembly_profile": projection.assembly_profile,
    }


def test_assemble_uses_projection_and_materialization(monkeypatch) -> None:
    expected = object()
    captured = []

    def materialize(projection):
        captured.append(projection)
        return expected

    monkeypatch.setattr(
        "chatcopilot.application.agent_runtime.materialize_agent_runtime",
        materialize,
    )

    result = assemble_agent_runtime(
        _runtime(),
        chat_config=ChatConfig(),
        profile=AgentRuntimeAssemblyProfile.DETACHED,
    )

    assert result is expected
    assert captured[0].tool_packs == ("workspace.read_write", "memory.chat")


def test_assembly_profiles_are_closed() -> None:
    assert set(AgentRuntimeAssemblyProfile.__members__) == {
        "INTERACTIVE",
        "DETACHED",
    }


def test_assembly_surface_has_no_unused_override_or_post_bind_axes() -> None:
    assert {field.name for field in fields(AgentRuntimeOverrides)} == {
        "tool_packs",
        "runtime_providers",
        "rag_sources",
        "mcp_servers",
        "subagents",
        "agent_backend",
    }
    assert not hasattr(AgentRuntime, "bind_payload_filter_factory")
    assert not hasattr(AgentRuntime, "bind_background_submitter_factory")
