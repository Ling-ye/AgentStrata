from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.agent.search.providers import SearchProviderRegistry
from chatcopilot.agent import runtime as runtime_module
from chatcopilot.agent.runtime import AgentRuntime, build_agent_runtime
from chatcopilot.agent.search.router import SearchRouter
from chatcopilot.agent.subagents.runner import SubagentRunner
from chatcopilot.agent.subagents.search_circuit import SearchCircuitBreaker
from chatcopilot.application.agent_runtime import (
    AgentRuntimeAssemblyProfile,
    AgentRuntimeOverrides,
    project_agent_runtime,
)
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.subagents import (
    CustomSubagentSpec,
    SearchProviderSpec,
    SubagentBudgetSpec,
    SubagentSpec,
    ToolSelectorSpec,
)
from chatcopilot.core.config import ChatConfig, LLMConfig, load_llm_profile
from chatcopilot.core.llm_client import LLMClient


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(llm=SimpleNamespace(
            research_model="research-default", research_env_prefix="RESEARCH",
        )),
        tool_packs=("search.unified", "agent.delegation"),
        exclude_tools=(), skills=(), rag_sources=(),
        mcp_servers=(McpServerConfig(id="search-source"),),
        subagents=SubagentSpec(
            include=("developer",),
            defaults=SubagentBudgetSpec(model_env_prefix="DELEGATE"),
            search_budget=SubagentBudgetSpec(model_env_prefix="SEARCH_AGENT"),
            research_enabled=True,
            research_budget=SubagentBudgetSpec(model_env_prefix="ROUTER"),
            custom=(CustomSubagentSpec(
                name="custom", tool_name="custom_agent", summary="Test agent",
                selector=ToolSelectorSpec(),
                budget=SubagentBudgetSpec(model_env_prefix="CUSTOM"),
            ),),
            search_providers=(SearchProviderSpec(id="web", kind="tavily"),),
        ),
        agent_backend="native",
    )


@pytest.mark.parametrize("profile", list(AgentRuntimeAssemblyProfile))
def test_projection_captures_instance_profiles_before_environment_changes(monkeypatch, profile):
    runtime = _runtime()
    cfg = ChatConfig(llm=LLMConfig(model="chat-a", api_key="test-a", timeout=31))
    env_a = {
        "RESEARCH_MODEL": "research-a", "ROUTER_MODEL": "router-a",
        "DELEGATE_MODEL": "delegate-a", "SEARCH_AGENT_MODEL": "search-a",
        "TAVILY_API_KEY": "credential-a", "CHATCOPILOT_SEARCH_QUOTA_MAX_TTL": "400",
    }
    first = project_agent_runtime(runtime, chat_config=cfg, profile=profile, environment=env_a)
    cfg.llm.model = "chat-b"
    runtime.subagents.agents["developer"] = SubagentBudgetSpec(model_env_prefix="OTHER")
    env_a["RESEARCH_MODEL"] = "edited"
    monkeypatch.setenv("ROUTER_MODEL", "unrelated-console-model")
    second = project_agent_runtime(runtime, chat_config=cfg, profile=profile, environment={})

    assert first.chat_config.llm.model == "chat-a"
    assert first.research_llm_config.model == "research-a"
    assert first.search_llm_config.model == "router-a"
    assert first.search_llm_config.timeout == 31
    profiles = dict(first.subagent_llm_configs)
    assert {key: value.model for key, value in profiles.items()} == {
        "CUSTOM": "chat-a", "DELEGATE": "delegate-a", "SEARCH_AGENT": "search-a",
    }
    assert all(value.api_key == "test-a" for value in profiles.values())
    assert first.search_provider_credentials == (("web", "credential-a"),)
    assert first.search_quota_max_ttl == 400
    assert not first.subagents.agents
    assert second.chat_config.llm.model == "chat-b"
    assert second.research_llm_config.model == "research-default"
    assert second.search_llm_config.model == "research-default"
    assert second.search_provider_credentials == (("web", ""),)
    assert second.search_quota_max_ttl == 86400
    assert "credential-a" not in repr(first)


def test_removed_capabilities_do_not_resolve_unused_profiles():
    runtime = _runtime()
    projection = project_agent_runtime(
        runtime, chat_config=ChatConfig(), environment={"DELEGATE_TIMEOUT": "invalid"},
        overrides=AgentRuntimeOverrides(subagents=SubagentSpec(), mcp_servers=()),
    )
    assert projection.subagent_llm_configs == ()
    assert projection.search_provider_credentials == ()


def test_explicit_empty_environment_does_not_inherit_process_profile(monkeypatch):
    monkeypatch.setenv("SLOT_MODEL", "other-instance")
    fallback = LLMConfig(model="configured")
    assert load_llm_profile("SLOT", fallback=fallback, environment={}).model == "configured"
    assert load_llm_profile("SLOT", fallback=fallback).model == "other-instance"
    assert load_llm_profile("SLOT", fallback=fallback, environment={"SLOT_MODEL": ""}) == fallback


def test_router_and_delegate_keep_injected_clients_after_environment_changes(monkeypatch):
    main = Mock(spec=LLMClient)
    router_client = Mock(spec=LLMClient)
    delegate_client = Mock(spec=LLMClient)
    clients = {"DELEGATE": delegate_client}
    runner = SubagentRunner(main_llm=main, main_config=ChatConfig(), tools=(), llm_profiles=clients)
    router = SearchRouter(main_llm=router_client, budget=SubagentBudgetSpec(model_env_prefix="ROUTER"))
    clients["DELEGATE"] = main
    monkeypatch.setenv("DELEGATE_MODEL", "late-model")
    monkeypatch.setenv("ROUTER_MODEL", "late-router")
    monkeypatch.setattr(LLMClient, "__init__", Mock(side_effect=AssertionError("execution built a client")))

    for _ in range(3):
        assert runner._resolve_llm("DELEGATE") is delegate_client
        assert runner._resolve_llm(None) is main
        assert router.resolve_llm() is router_client
    with pytest.raises(ValueError, match="profile was not materialized"):
        runner._resolve_llm("UNKNOWN")


def test_provider_credentials_are_instance_bound_and_not_read_from_process(monkeypatch):
    spec = SearchProviderSpec(id="web", kind="tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "ambient")
    a = SearchProviderRegistry.from_tools((), provider_specs=(spec,), provider_credentials={"web": "a"})
    b = SearchProviderRegistry.from_tools((), provider_specs=(spec,), provider_credentials={"web": "b"})
    absent = SearchProviderRegistry.from_tools((), provider_specs=(spec,))
    assert a._in_process["web"]._credential == "a"
    assert b._in_process["web"]._credential == "b"
    assert absent._unavailable == {"web": "search_credential_missing"}


def test_quota_circuit_cap_is_instance_scoped():
    now = [0.0]
    short = SearchCircuitBreaker(clock=lambda: now[0], quota_max_ttl=10)
    long = SearchCircuitBreaker(clock=lambda: now[0], quota_max_ttl=20)
    short.record_failure("web", "mcp_quota_exceeded")
    long.record_failure("web", "mcp_quota_exceeded")
    now[0] = 11
    assert short.blocked("web") is None
    assert long.blocked("web") == "mcp_quota_exceeded"


def test_llm_client_copies_config_preserves_shared_limiter_and_closes_once(monkeypatch, tmp_path):
    transports = []

    def build(_self):
        transport = SimpleNamespace(close=Mock())
        transports.append(transport)
        return transport

    monkeypatch.setattr(LLMClient, "_build_client", build)
    monkeypatch.setenv("CHATCOPILOT_LIMIT_DIR", str(tmp_path / "shared"))
    monkeypatch.setenv("CHATCOPILOT_LLM_CONCURRENCY", "2")
    cfg = LLMConfig(model="a")
    first = LLMClient(cfg)
    second = LLMClient(replace(cfg, model="b"))
    cfg.model = "changed"
    assert first.model == "a"
    assert first._limiter.root == second._limiter.root == tmp_path / "shared" / "llm"
    assert first._limiter.max_concurrency == second._limiter.max_concurrency == 2
    first.close()
    first.close()
    transports[0].close.assert_called_once_with()
    transports[1].close.assert_not_called()
    with pytest.raises(RuntimeError, match="closed"):
        first.chat([])
    second.close()


def test_runtime_reuses_equal_profiles_only_within_each_instance(monkeypatch):
    created = []

    def client(config):
        value = SimpleNamespace(config=replace(config), close=Mock())
        created.append(value)
        return value

    monkeypatch.setattr(runtime_module, "LLMClient", client)
    chat = LLMConfig(model="chat")
    research = replace(chat, model="research")
    options = {
        "chat_config": ChatConfig(llm=chat), "research_llm_config": research,
        "search_llm_config": replace(research),
        "subagent_llm_configs": (("CHAT", replace(chat)), ("RESEARCH", replace(research))),
        "tool_packs": (),
    }
    first = build_agent_runtime(**options)
    second = build_agent_runtime(**options, assembly_profile="detached")

    assert len(created) == 4
    assert first.llm is first.subagent_llms["CHAT"]
    assert first.research_llm is first.search_llm is first.subagent_llms["RESEARCH"]
    assert first.llm is not second.llm
    assert first.research_llm is not second.research_llm
    first.close()
    first.close()
    for item in created[:2]:
        item.close.assert_called_once_with()
    for item in created[2:]:
        item.close.assert_not_called()
    second.close()
    for item in created[2:]:
        item.close.assert_called_once_with()


@pytest.mark.parametrize("failure", ["model", "retriever", "mcp_constructor", "mcp_load"])
def test_runtime_assembly_closes_completed_resources_after_failure(monkeypatch, failure):
    created = []
    retriever = SimpleNamespace(close=Mock())
    mcp = SimpleNamespace(close=Mock(), load_provider=Mock(side_effect=RuntimeError("mcp_load")))

    def client(config):
        if failure == "model" and created:
            raise RuntimeError("model")
        item = SimpleNamespace(config=replace(config), close=Mock())
        created.append(item)
        return item

    def create_retriever(_sources):
        if failure == "retriever":
            raise RuntimeError("retriever")
        return retriever

    def create_mcp(_configs):
        if failure == "mcp_constructor":
            raise RuntimeError("mcp_constructor")
        return mcp

    monkeypatch.setattr(runtime_module, "LLMClient", client)
    monkeypatch.setattr(runtime_module, "LocalTextRetriever", create_retriever)
    monkeypatch.setattr(runtime_module, "McpToolProvider", create_mcp)
    with pytest.raises(RuntimeError, match=failure):
        build_agent_runtime(
            chat_config=ChatConfig(), research_llm_config=LLMConfig(model="different"),
            tool_packs=(), rag_sources=(object(),), mcp_servers=(McpServerConfig(id="test"),),
        )
    assert len(created) == (1 if failure == "model" else 2)
    for item in created:
        item.close.assert_called_once_with()
    if failure in {"mcp_constructor", "mcp_load"}:
        retriever.close.assert_called_once_with()
    if failure == "mcp_load":
        mcp.close.assert_called_once_with()


def test_runtime_close_continues_after_one_resource_fails():
    llm = SimpleNamespace(close=Mock())
    mcp = SimpleNamespace(close=Mock(side_effect=RuntimeError("close failed")))
    runtime = AgentRuntime(
        llm=llm, tools=(), tools_schema=(), runtime_config=ChatConfig(), mcp_provider=mcp,
        subagent_llms={"ALIAS": llm},
    )
    with pytest.raises(RuntimeError, match="close failed"):
        runtime.close()
    llm.close.assert_called_once_with()
    runtime.close()
    mcp.close.assert_called_once_with()


def test_direct_runtime_defaults_do_not_resolve_ambient_profiles(monkeypatch):
    client = SimpleNamespace(close=Mock())
    monkeypatch.setenv("RESEARCH_MODEL", "ambient")
    monkeypatch.setenv("CHATCOPILOT_SEARCH_QUOTA_MAX_TTL", "invalid")
    monkeypatch.setattr(
        "chatcopilot.core.config.load_llm_profile",
        Mock(side_effect=AssertionError("direct runtime read a profile")),
    )
    runtime = AgentRuntime(llm=client, tools=(), tools_schema=(), runtime_config=ChatConfig())
    assert runtime.research_llm is runtime.search_llm is client
    assert not runtime.subagent_llms
    runtime.close()


@pytest.mark.parametrize("kind", ["subagent", "router"])
def test_unresolved_model_override_is_rejected_before_materialization(monkeypatch, kind):
    create = Mock(side_effect=AssertionError("constructed before validation"))
    monkeypatch.setattr(runtime_module, "LLMClient", create)
    if kind == "subagent":
        subagents = SubagentSpec(include=("developer",), defaults=SubagentBudgetSpec(model_env_prefix="MISSING"))
    else:
        subagents = SubagentSpec(research_enabled=True, research_budget=SubagentBudgetSpec(model_env_prefix="MISSING"))
    with pytest.raises(ValueError, match="(?i)(profile|resolved|materializ)"):
        build_agent_runtime(chat_config=ChatConfig(), tool_packs=(), subagents=subagents)
    create.assert_not_called()
