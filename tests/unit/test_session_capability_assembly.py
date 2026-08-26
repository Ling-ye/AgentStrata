from __future__ import annotations

from types import MappingProxyType, ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from chatcopilot.agent import runtime as runtime_module
from chatcopilot.agent.capabilities import assembly as capability_assembly
from chatcopilot.agent.capabilities.assembly import (
    CapabilityMaterializationError,
    RuntimeCapabilityContext,
    SessionCapabilityContext,
    materialize_runtime_providers,
    materialize_session_providers,
)
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.tools.registry import ToolMaterializationError
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.component_catalog.audit import audit_component_catalog
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.tool_packs import ToolPackEntry, ToolProvider
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.core.config import ChatConfig, LLMConfig
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.tool_packs.catalog import (
    get_tool_pack_entry,
    project_tool_pack_names,
    session_tool_pack_entries,
)
from chatcopilot.tool_packs import catalog as tool_pack_catalog


def _tool(name: str) -> ToolDef:
    def handler(_arguments: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary="ok", data={})

    return ToolDef(
        name=name,
        summary="Capability assembly test tool.",
        input_schema=object_schema({}),
        output_schema=object_schema({}),
        handler=handler,
        category="tests.capability",
        owner="tests",
        module=__name__,
        artifact_kinds=(),
    )


def _context() -> SessionCapabilityContext:
    llm = cast(LLMClient, object())
    return SessionCapabilityContext(
        session_id="session-1",
        backend_id="native",
        main_llm=llm,
        research_llm=llm,
        runtime_config=ChatConfig(),
        subagents=SubagentSpec(),
        base_tools=(),
        subagent_tools=(),
        mcp_configs=(),
        memory_snapshot="",
        retriever=None,
        search_circuit=SearchCircuitBreaker(),
    )


def test_catalog_declares_ordered_agent_session_contributors() -> None:
    entries = session_tool_pack_entries()

    assert tuple(entry.name for entry in entries) == (
        "agent.delegation",
        "search.unified",
    )
    assert all(entry.provider_factory_module for entry in entries)
    assert all(entry.runtime_scope == "agent_session" for entry in entries)


def test_catalog_filters_session_contributors_by_profile_and_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_entry = ToolPackEntry(
        name="tests.default",
        dynamic=True,
        description="Compatibility-default session capability.",
        runtime_scope="agent_session",
        provider_factory_module="chatcopilot.agent.capabilities.tests_default",
        factory_order=10,
        session_default_enabled=True,
    )
    interactive_entry = ToolPackEntry(
        name="tests.interactive",
        dynamic=True,
        description="Explicit interactive-only session capability.",
        runtime_scope="agent_session",
        projection_profiles=("interactive",),
        provider_factory_module="chatcopilot.agent.capabilities.tests_interactive",
        factory_order=20,
    )
    monkeypatch.setattr(
        tool_pack_catalog,
        "BUILTIN_TOOL_PACKS",
        MappingProxyType(
            {
                default_entry.name: default_entry,
                interactive_entry.name: interactive_entry,
            }
        ),
    )

    assert tuple(
        entry.name
        for entry in session_tool_pack_entries((), profile="detached")
    ) == ("tests.default",)
    assert tuple(
        entry.name
        for entry in session_tool_pack_entries(
            ("tests.interactive",),
            profile="interactive",
        )
    ) == ("tests.default", "tests.interactive")
    assert tuple(
        entry.name
        for entry in session_tool_pack_entries(
            ("tests.interactive",),
            profile="detached",
        )
    ) == ("tests.default",)


def test_materializer_fails_closed_for_missing_builder_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = ToolPackEntry(
        name="tests.session",
        dynamic=True,
        description="Injected test capability.",
        runtime_scope="agent_session",
        provider_factory_module="chatcopilot.agent.capabilities.tests",
        factory_order=10,
    )
    monkeypatch.setattr(
        capability_assembly,
        "session_tool_pack_entries",
        lambda *_args, **_kwargs: (entry,),
    )
    module = ModuleType(entry.provider_factory_module)
    module.custom_provider = lambda _context: ToolProvider(
        id="tests.session",
        packs={"tests.session": (_tool("wrong_export"),)},
        module=module.__name__,
    )

    with pytest.raises(CapabilityMaterializationError) as caught:
        materialize_session_providers(
            _context(),
            module_loader=lambda _name: module,
        )

    assert caught.value.pack_id == "tests.session"
    assert caught.value.reason == "builder_export_missing"


def test_session_materialization_preserves_contributor_order_and_allows_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ToolPackEntry(
        name="tests.first",
        dynamic=True,
        description="First injected capability.",
        runtime_scope="agent_session",
        provider_factory_module="chatcopilot.agent.capabilities.tests_first",
        factory_order=10,
    )
    second = ToolPackEntry(
        name="tests.second",
        dynamic=True,
        description="Second optional capability.",
        runtime_scope="agent_session",
        provider_factory_module="chatcopilot.agent.capabilities.tests_second",
        factory_order=20,
    )
    monkeypatch.setattr(
        capability_assembly,
        "session_tool_pack_entries",
        lambda *_args, **_kwargs: (first, second),
    )
    first_module = ModuleType(first.provider_factory_module)
    second_module = ModuleType(second.provider_factory_module)

    def build_first(context: SessionCapabilityContext) -> ToolProvider:
        assert context.session_id == "session-1"
        assert context.contributed_tools == ()
        return ToolProvider(
            id=first.name,
            packs={first.name: (_tool("first_tool"),)},
            module=first_module.__name__,
        )

    def build_second(context: SessionCapabilityContext) -> None:
        assert tuple(tool.name for tool in context.contributed_tools) == ("first_tool",)
        return None

    first_module.build_provider = build_first
    second_module.build_provider = build_second
    modules = {
        first_module.__name__: first_module,
        second_module.__name__: second_module,
    }

    providers = materialize_session_providers(
        _context(),
        module_loader=modules.__getitem__,
    )

    assert tuple(provider.id for provider in providers) == (first.name,)
    assert providers[0].packs[first.name][0].name == "first_tool"


def test_runtime_materialization_rejects_none_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = ToolPackEntry(
        name="tests.runtime",
        dynamic=True,
        description="Runtime capability.",
        runtime_scope="runtime",
        provider_factory_module="chatcopilot.agent.capabilities.tests_runtime",
        factory_order=10,
    )
    monkeypatch.setattr(
        capability_assembly,
        "get_tool_pack_entry",
        lambda _name: entry,
    )
    module = ModuleType(entry.provider_factory_module)
    module.build_provider = lambda _context: None

    with pytest.raises(CapabilityMaterializationError) as caught:
        materialize_runtime_providers(
            (entry.name,),
            RuntimeCapabilityContext(),
            module_loader=lambda _name: module,
        )

    assert caught.value.pack_id == entry.name
    assert caught.value.reason == "builder_result_invalid"


def test_runtime_threads_detached_profile_into_session_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopAfterProjection(RuntimeError):
        pass

    def capture_projection(_context: SessionCapabilityContext, **kwargs: Any) -> tuple:
        assert kwargs["tool_pack_names"] == ("tests.session",)
        assert kwargs["profile"] == "detached"
        raise StopAfterProjection

    monkeypatch.setattr(
        runtime_module,
        "materialize_session_providers",
        capture_projection,
    )
    llm = cast(LLMClient, object())
    runtime = AgentRuntime(
        llm=llm,
        tools=(),
        tools_schema=(),
        runtime_config=ChatConfig(),
        assembly_profile="detached",
        session_capability_packs=("tests.session",),
    )

    with pytest.raises(StopAfterProjection):
        runtime.new_session(
            session_id="session-1",
            prompt_input=cast(Any, SimpleNamespace(memory="")),
        )


def test_direct_runtime_does_not_select_future_session_capabilities_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopAfterProjection(RuntimeError):
        pass

    def capture_projection(_context: SessionCapabilityContext, **kwargs: Any) -> tuple:
        assert kwargs["tool_pack_names"] == ()
        assert kwargs["profile"] == "interactive"
        raise StopAfterProjection

    monkeypatch.setattr(
        runtime_module,
        "materialize_session_providers",
        capture_projection,
    )
    runtime = AgentRuntime(
        llm=cast(LLMClient, object()),
        tools=(),
        tools_schema=(),
        runtime_config=ChatConfig(),
    )

    with pytest.raises(StopAfterProjection):
        runtime.new_session(
            session_id="session-1",
            prompt_input=cast(Any, SimpleNamespace(memory="")),
        )


def test_invalid_profile_fails_before_mcp_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_calls: list[str] = []

    def forbidden_mcp_provider(_configs: tuple) -> object:
        mcp_calls.append("initialized")
        raise AssertionError("MCP must not initialize for an invalid profile")

    monkeypatch.setattr(runtime_module, "McpToolProvider", forbidden_mcp_provider)

    with pytest.raises(ValueError, match="assembly profile"):
        runtime_module.build_agent_runtime(
            chat_config=ChatConfig(),
            mcp_servers=(cast(Any, object()),),
            assembly_profile=cast(Any, "invalid"),
        )

    assert mcp_calls == []


def test_detached_runtime_rejects_reintroduced_interactive_provider() -> None:
    persona_provider = ToolProvider(
        id="tests.persona",
        packs={"persona.control": (_tool("persona_reintroduced"),)},
        module=__name__,
    )

    with pytest.raises(ValueError, match="persona.control"):
        runtime_module.build_agent_runtime(
            chat_config=ChatConfig(),
            tool_packs=(),
            runtime_providers=(persona_provider,),
            assembly_profile="detached",
        )


def test_detached_runtime_allows_unknown_local_provider_pack() -> None:
    local_provider = ToolProvider(
        id="tests.fixture",
        packs={"tests.fixture": (_tool("local_fixture"),)},
        module=__name__,
    )

    runtime = runtime_module.build_agent_runtime(
        chat_config=ChatConfig(llm=LLMConfig(api_key="test-key")),
        tool_packs=(),
        runtime_providers=(local_provider,),
        assembly_profile="detached",
    )

    assert tuple(tool.name for tool in runtime.tools) == ("local_fixture",)


def test_runtime_closes_mcp_when_loaded_provider_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = {"loaded": 0, "closed": 0}

    class InvalidMcpProvider:
        def __init__(self, _configs: tuple) -> None:
            pass

        def load_provider(self) -> ToolProvider:
            lifecycle["loaded"] += 1
            invalid = _tool("invalid_mcp_tool")
            invalid.audiences = cast(Any, ("worker",))
            return ToolProvider(
                id="tests.invalid-mcp",
                packs={"mcp.dynamic": (invalid,)},
                module=__name__,
            )

        def close(self) -> None:
            lifecycle["closed"] += 1

    monkeypatch.setattr(runtime_module, "McpToolProvider", InvalidMcpProvider)

    with pytest.raises(ToolMaterializationError, match="invalid_tool_audiences"):
        runtime_module.build_agent_runtime(
            chat_config=ChatConfig(llm=LLMConfig(api_key="test-key")),
            tool_packs=(),
            mcp_servers=(cast(Any, object()),),
        )

    assert lifecycle == {"loaded": 1, "closed": 1}


def test_successful_runtime_owns_mcp_until_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = {"loaded": 0, "closed": 0}

    class ValidMcpProvider:
        def __init__(self, _configs: tuple) -> None:
            pass

        def load_provider(self) -> ToolProvider:
            lifecycle["loaded"] += 1
            return ToolProvider(
                id="tests.valid-mcp",
                packs={"mcp.dynamic": (_tool("valid_mcp_tool"),)},
                module=__name__,
            )

        def close(self) -> None:
            lifecycle["closed"] += 1

    monkeypatch.setattr(runtime_module, "McpToolProvider", ValidMcpProvider)

    runtime = runtime_module.build_agent_runtime(
        chat_config=ChatConfig(llm=LLMConfig(api_key="test-key")),
        tool_packs=(),
        mcp_servers=(cast(Any, object()),),
    )

    assert lifecycle == {"loaded": 1, "closed": 0}
    runtime.close()
    assert lifecycle == {"loaded": 1, "closed": 1}


def test_detached_projection_excludes_host_persona_by_catalog_trait() -> None:
    selected = ("workspace.read_write", "persona.control", "playbooks.reader")

    assert project_tool_pack_names(selected, profile="interactive") == selected
    assert project_tool_pack_names(selected, profile="detached") == (
        "workspace.read_write",
        "playbooks.reader",
    )
    assert get_tool_pack_entry("persona.control").runtime_scope == "host_session"  # type: ignore[union-attr]


def test_projection_rejects_unknown_profile_and_pack() -> None:
    with pytest.raises(ValueError, match="projection profile"):
        project_tool_pack_names(("workspace.read_write",), profile="unknown")
    with pytest.raises(ValueError, match="unknown tool pack"):
        project_tool_pack_names(("missing.pack",), profile="detached")


def test_catalog_audit_rejects_missing_factory_and_invalid_projection() -> None:
    entry = ToolPackEntry(
        name="tests.session",
        dynamic=True,
        description="Malformed session capability.",
        runtime_scope="agent_session",
        projection_profiles=("interactive", "unknown"),
    )

    report = audit_component_catalog(
        tool_packs={entry.name: entry},
        tool_features={},
        mcp_entries={},
        subagents={},
        workflows={},
    )

    assert {issue.code for issue in report.issues} >= {
        "tool_pack.projection_profiles_invalid",
        "tool_pack.provider_factory_missing",
    }


def test_catalog_audit_requires_fixed_build_provider_export() -> None:
    entry = ToolPackEntry(
        name="tests.session",
        dynamic=True,
        description="Malformed session capability.",
        runtime_scope="agent_session",
        provider_factory_module="chatcopilot.agent.capabilities.tests",
        factory_order=10,
    )
    module = ModuleType(entry.provider_factory_module)
    module.custom_provider = lambda _context: None

    report = audit_component_catalog(
        tool_packs={entry.name: entry},
        tool_features={},
        mcp_entries={},
        subagents={},
        workflows={},
        module_loader=lambda _name: module,
    )

    assert "tool_pack.provider_factory_export_invalid" in {
        issue.code for issue in report.issues
    }


def test_catalog_audit_rejects_session_default_on_non_session_pack() -> None:
    entry = ToolPackEntry(
        name="tests.static",
        provider_module="chatcopilot.agent.tools.builtin.memory_tools",
        description="Malformed compatibility default.",
        session_default_enabled=True,
    )

    report = audit_component_catalog(
        tool_packs={entry.name: entry},
        tool_features={},
        mcp_entries={},
        subagents={},
        workflows={},
    )

    assert "tool_pack.session_default_invalid" in {
        issue.code for issue in report.issues
    }
