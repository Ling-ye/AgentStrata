from __future__ import annotations

from tests.prompt_plan_fixture import prompt_input

import json
from unittest.mock import Mock, patch

import pytest

from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.agent.research.models import ResearchRequest
from chatcopilot.agent.research.router import ResearchRouter
from chatcopilot.agent.research.runtime import build_research_tool
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import SubagentBudgetSpec, SubagentSpec
from chatcopilot.contracts.agent_backend import CodexMainSessionPolicy
from chatcopilot.contracts.subagents import SearchProviderSpec
from chatcopilot.external_tools.shared.tool_spec import ToolDef


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []
        self.model = "fake-router"

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return ChatResult(content=self.content, finish_reason="stop")


class _ScriptedLLM(_FakeLLM):
    def __init__(self, contents: list[str]) -> None:
        super().__init__(contents[0])
        self.contents = contents

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents[min(len(self.calls) - 1, len(self.contents) - 1)]
        return ChatResult(content=content, finish_reason="stop")


def _tool(name: str, summary: str = "ok", *, metadata: dict | None = None) -> ToolDef:
    return ToolDef(
        name=name,
        summary=name,
        properties={},
        required=[],
        handler=lambda _args: (summary, [], None),
        metadata=dict(metadata or {}),
    )


def test_router_emits_validated_complete_plan() -> None:
    llm = _FakeLLM(
        json.dumps(
            {
                "operation": "mixed",
                "steps": [
                    {
                        "source": "web",
                        "query": "Unity latest release",
                        "required_fields": ["title", "url", "date"],
                        "read_strategy": "search_then_read",
                    },
                    {
                        "source": "github",
                        "query": "Unity release repository",
                        "required_fields": ["url", "release"],
                        "read_strategy": "search_only",
                    },
                ],
                "cross_check": False,
                "reason": "official and repository evidence",
            }
        )
    )
    router = ResearchRouter(
        main_llm=llm,
        budget=SubagentBudgetSpec(timeout_seconds=15),
    )

    plan = router.route(
        ResearchRequest(
            objective="compare Unity release notes versus GitHub releases",
            required_fields=("title", "url", "date"),
            depth="thorough",
        ),
        available_sources=("web", "github"),
    )

    assert plan.operation == "mixed"
    assert [step.source for step in plan.steps] == ["web", "github"]
    assert plan.cross_check is True
    assert llm.calls[0]["tools"] is None
    assert llm.calls[0]["stream"] is False
    assert llm.calls[0]["max_retries"] == 0
    assert llm.calls[0]["timeout"] == 15


def test_router_preserves_explicit_url_and_source_hint() -> None:
    llm = _FakeLLM(
        json.dumps(
            {
                "operation": "search",
                "steps": [{"source": "web", "query": "ignored"}],
                "cross_check": False,
            }
        )
    )
    router = ResearchRouter(
        main_llm=llm,
        budget=SubagentBudgetSpec(),
    )
    request = ResearchRequest(
        objective="读取并比较",
        urls=("https://example.com/page",),
        source_hints=("experience",),
    )

    plan = router.route(
        request,
        available_sources=("web", "experience", "url"),
    )

    assert [step.source for step in plan.steps] == ["url", "experience"]
    assert plan.steps[0].urls == request.urls
    assert plan.route_source == "script"
    assert llm.calls == []


def test_router_explicit_url_uses_deterministic_plan() -> None:
    llm = _FakeLLM("not json")
    router = ResearchRouter(
        main_llm=llm,
        budget=SubagentBudgetSpec(),
    )

    plan = router.route(
        ResearchRequest(
            objective="读取这个页面",
            urls=("https://example.com",),
        ),
        available_sources=("web", "url"),
    )

    assert plan.route_source == "script"
    assert plan.operation == "read_url"
    assert plan.steps[0].source == "url"
    assert llm.calls == []


def test_request_rejects_more_sources_than_plan_can_preserve() -> None:
    with pytest.raises(ValueError, match="at most 3"):
        ResearchRequest.from_args(
            {
                "objective": "compare everything",
                "urls": ["https://example.com"],
                "source_hints": ["web", "experience", "commerce"],
            }
        )


def test_request_rejects_url_hint_without_concrete_url() -> None:
    with pytest.raises(ValueError, match="requires at least one concrete URL"):
        ResearchRequest.from_args(
            {
                "objective": "read a page",
                "source_hints": ["url"],
            }
        )


def test_router_does_not_cache_transient_fallback() -> None:
    llm = _ScriptedLLM(
        [
            "not json",
            json.dumps(
                {
                    "operation": "search",
                    "steps": [{"source": "web", "query": "second attempt"}],
                }
            ),
        ]
    )
    router = ResearchRouter(main_llm=llm, budget=SubagentBudgetSpec())
    request = ResearchRequest(
        objective="compare release notes versus changelog",
        depth="thorough",
    )

    first = router.route(request, available_sources=("web",))
    second = router.route(request, available_sources=("web",))

    assert first.route_source == "fallback"
    assert second.route_source == "llm"
    assert len(llm.calls) == 2


def test_router_rejects_hallucinated_url_and_recomputes_operation() -> None:
    llm = _FakeLLM(
        json.dumps(
            {
                "operation": "read_url",
                "steps": [
                    {
                        "source": "url",
                        "urls": ["https://invented.example/page"],
                    },
                    {"source": "web", "query": "real query"},
                ],
            }
        )
    )
    router = ResearchRouter(main_llm=llm, budget=SubagentBudgetSpec())

    plan = router.route(
        ResearchRequest(
            objective="compare the real page versus release notes",
            depth="thorough",
        ),
        available_sources=("web", "url"),
    )

    assert [step.source for step in plan.steps] == ["web"]
    assert plan.operation == "search"


def test_verification_none_disables_discretionary_cross_check() -> None:
    llm = _FakeLLM(
        json.dumps(
            {
                "operation": "search",
                "steps": [{"source": "web", "query": "stable fact"}],
                "cross_check": True,
            }
        )
    )
    router = ResearchRouter(main_llm=llm, budget=SubagentBudgetSpec())

    plan = router.route(
        ResearchRequest(
            objective="compare stable fact versus prior result",
            verification="none",
            depth="thorough",
        ),
        available_sources=("web",),
    )

    assert plan.cross_check is False


def test_research_runner_upgrades_javascript_shell_to_browser() -> None:
    llm = _FakeLLM(
        json.dumps(
            {
                "operation": "read_url",
                "steps": [
                    {
                        "source": "url",
                        "query": "read boss page",
                        "urls": ["https://tarkov.dev/boss/cultist-warrior"],
                        "required_fields": ["name", "health"],
                        "read_strategy": "static_then_browser",
                    }
                ],
                "cross_check": False,
            }
        )
    )
    static = _tool(
        "web_fetch_page",
        "Title: Tarkov.dev\nURL: https://tarkov.dev/boss/cultist-warrior\n"
        "Content:\nPlease enable JavaScript to continue.",
    )
    dynamic = _tool(
        "browse_dynamic_page",
        json.dumps(
            {
                "ok": True,
                "summary": "Cultist warrior health is 850",
                "evidence": [{"url": "https://tarkov.dev/boss/cultist-warrior"}],
            }
        ),
    )
    research = build_research_tool(
        main_llm=llm,
        budget=SubagentBudgetSpec(),
        tools=(static, dynamic),
    )
    assert research is not None

    result = ToolExecutor(tools=[research]).execute(
        "research_information",
        {
            "objective": "读取邪教徒战士的生命值",
            "urls": ["https://tarkov.dev/boss/cultist-warrior"],
            "required_fields": ["name", "health"],
        },
    )
    payload = json.loads(result.summary)

    assert payload["ok"] is True
    assert payload["results"][0]["pages"][0]["method"] == "dynamic"
    assert payload["results"][0]["pages"][0]["actual_source"] == "playwright"


def test_cross_check_uses_searxng_after_tavily_fallback() -> None:
    llm = _FakeLLM(
        json.dumps(
            {
                "operation": "search",
                "steps": [{"source": "web", "query": "latest release"}],
                "cross_check": True,
            }
        )
    )
    secondary_calls: list[dict] = []
    primary = _tool(
        "search_tavily",
        json.dumps(
            {
                "ok": True,
                "summary": "fallback result",
                "fallback": {"source": "searxng"},
            }
        ),
    )
    secondary = ToolDef(
        name="search_searxng",
        summary="search_searxng",
        properties={},
        required=[],
        handler=lambda args: (
            secondary_calls.append(args) or json.dumps({"ok": True}),
            [],
            None,
        ),
    )
    research = build_research_tool(
        main_llm=llm,
        budget=SubagentBudgetSpec(),
        tools=(primary, secondary),
    )
    assert research is not None

    result = ToolExecutor(tools=[research]).execute(
        "research_information",
        {"objective": "latest release"},
    )
    payload = json.loads(result.summary)

    assert payload["ok"] is True
    assert payload["actual_sources"] == ["tavily:fallback_searxng", "searxng"]
    assert payload["limits"]["cross_check_completed"] is True
    assert len(secondary_calls) == 1


def test_runtime_hides_internal_information_tools_when_research_enabled() -> None:
    web_fetch = _tool("web_fetch_page")
    delegates = (
        _tool("search_tavily", metadata={"subagent_kind": "search"}),
        _tool("search_xiaohongshu", metadata={"subagent_kind": "search"}),
        _tool("query_approved_sources", metadata={"subagent_kind": "external"}),
        _tool("browse_dynamic_page", metadata={"subagent_kind": "external"}),
    )
    runtime = AgentRuntime(
        llm=_FakeLLM("{}"),
        tools=(web_fetch, _tool("normal_tool")),
        tools_schema=(),
        runtime_config=ChatConfig(),
        subagents=SubagentSpec(
            research_enabled=True,
            research_budget=SubagentBudgetSpec(),
        ),
    )

    with patch(
        "chatcopilot.agent.runtime.build_subagent_tools",
        return_value=delegates,
    ):
        session = runtime.new_session(session_id="sid", prompt_input=prompt_input("base"))

    concrete = session.backend.native_session(session.backend_session_ref)
    names = {entry["function"]["name"] for entry in concrete.tools_schema}
    assert "search_information" in names
    assert "normal_tool" in names
    assert "search_tavily" not in names
    assert "search_xiaohongshu" not in names
    assert "query_approved_sources" not in names
    assert "web_fetch_page" not in names
    assert "browse_dynamic_page" not in names
    assert concrete.prompt_plan.tool_projection_digest


@pytest.mark.parametrize("backend", ["native", "langgraph"])
def test_native_and_langgraph_expose_search_information_for_direct_provider(
    backend: str,
) -> None:
    provider = SearchProviderSpec(
        id="searxng",
        kind="searxng",
        endpoint="http://127.0.0.1:18064",
    )
    runtime = AgentRuntime(
        llm=_FakeLLM("{}"),
        tools=(_tool("normal_tool"),),
        tools_schema=(),
        runtime_config=ChatConfig(),
        subagents=SubagentSpec(
            research_enabled=True,
            research_budget=SubagentBudgetSpec(),
            search_providers=(provider,),
        ),
        agent_backend=backend,
    )

    with patch("chatcopilot.agent.runtime.build_subagent_tools", return_value=()):
        session = runtime.new_session(session_id=f"sid-{backend}", prompt_input=prompt_input("base"))

    assert "search_information" in session.capabilities.tool_names


def test_codex_backend_does_not_construct_chatcopilot_search_or_delegate_agents() -> None:
    from chatcopilot.contracts.agent_backend import (
        BackendCapabilities,
        BackendSessionRef,
        CAPABILITY_CHAT,
        CAPABILITY_TOOLS,
    )

    backend = Mock()
    backend.capabilities = BackendCapabilities(
        names=frozenset({CAPABILITY_CHAT, CAPABILITY_TOOLS}),
        tool_names=frozenset({"normal_tool"}),
    )
    backend.open_session.return_value = BackendSessionRef("codex", "native-session")
    runtime = AgentRuntime(
        llm=_FakeLLM("{}"),
        tools=(_tool("normal_tool"),),
        tools_schema=(),
        runtime_config=ChatConfig(),
        subagents=SubagentSpec(
            include=("browser_reader",),
            research_enabled=True,
            research_budget=SubagentBudgetSpec(),
            search_providers=(
                SearchProviderSpec(id="searxng", kind="searxng"),
            ),
        ),
        agent_backend="codex",
    )

    with patch("chatcopilot.agent.runtime.build_subagent_tools") as delegates, patch(
        "chatcopilot.agent.runtime.build_search_tool"
    ) as search, patch(
        "chatcopilot.agent.runtime.build_backend", return_value=backend
    ):
        session = runtime.new_session(session_id="sid-codex", prompt_input=prompt_input("base"))

    delegates.assert_not_called()
    search.assert_not_called()
    request = backend.open_session.call_args.args[0]
    assert request.allowed_tool_names == frozenset({"normal_tool"})
    assert session.capabilities.tool_names == frozenset({"normal_tool"})


def test_codex_eval_policy_exposes_real_unified_search_tool() -> None:
    from chatcopilot.contracts.agent_backend import (
        BackendCapabilities,
        BackendSessionRef,
        CAPABILITY_CHAT,
        CAPABILITY_TOOLS,
    )

    backend = Mock()
    backend.capabilities = BackendCapabilities(
        names=frozenset({CAPABILITY_CHAT, CAPABILITY_TOOLS}),
        tool_names=frozenset({"normal_tool", "search_information"}),
    )
    backend.open_session.return_value = BackendSessionRef("codex", "native-session")
    runtime = AgentRuntime(
        llm=_FakeLLM("{}"),
        tools=(_tool("normal_tool"),),
        tools_schema=(),
        runtime_config=ChatConfig(),
        subagents=SubagentSpec(
            research_enabled=True,
            research_budget=SubagentBudgetSpec(),
            search_providers=(
                SearchProviderSpec(
                    id="searxng",
                    kind="searxng",
                    endpoint="http://127.0.0.1:18064",
                ),
            ),
            codex=CodexMainSessionPolicy(allow_unified_search_tool=True),
        ),
        agent_backend="codex",
    )

    with patch(
        "chatcopilot.agent.runtime.build_subagent_tools",
        return_value=(),
    ), patch(
        "chatcopilot.agent.runtime.build_backend",
        return_value=backend,
    ):
        session = runtime.new_session(
            session_id="sid-codex-eval-search",
            prompt_input=prompt_input("base"),
        )

    request = backend.open_session.call_args.args[0]
    assert request.allowed_tool_names == frozenset(
        {"normal_tool", "search_information"}
    )
    assert session.capabilities.tool_names == frozenset(
        {"normal_tool", "search_information"}
    )


def test_codex_backend_uses_current_personal_workspace_root(tmp_path) -> None:
    from chatcopilot.contracts.agent_backend import (
        BackendCapabilities,
        BackendSessionRef,
        CAPABILITY_CHAT,
        CAPABILITY_TOOLS,
    )

    instance_root = tmp_path / "instance"
    personal_root = instance_root / "p2p_123"
    personal_root.mkdir(parents=True)
    workspace = Mock(root=personal_root)
    workspace_service = Mock()
    workspace_service.resolve_workspace.return_value = workspace
    workspace_service.resolve_workspace_root.return_value = instance_root
    backend = Mock()
    backend.capabilities = BackendCapabilities(
        names=frozenset({CAPABILITY_CHAT, CAPABILITY_TOOLS}),
        tool_names=frozenset({"normal_tool"}),
    )
    backend.open_session.return_value = BackendSessionRef("codex", "native-session")
    runtime = AgentRuntime(
        llm=_FakeLLM("{}"),
        tools=(_tool("normal_tool"),),
        tools_schema=(),
        runtime_config=ChatConfig(),
        agent_backend="codex",
    )

    with patch("chatcopilot.agent.runtime.build_subagent_tools", return_value=()), patch(
        "chatcopilot.agent.runtime.build_backend", return_value=backend
    ):
        runtime.new_session(
            session_id="sid-personal-workspace",
            prompt_input=prompt_input("base"),
            workspace_service=workspace_service,
        )

    request = backend.open_session.call_args.args[0]
    assert request.options["workspace_root"] == personal_root.resolve()
    assert request.options["backend_state_root"] == (
        personal_root.resolve() / ".backend-sessions"
    )
    workspace_service.resolve_workspace.assert_called_once_with(create=True)
    workspace_service.resolve_workspace_root.assert_not_called()


def test_runtime_permission_filter_prevents_url_read_bypass() -> None:
    fetch_calls: list[dict] = []
    web_fetch = ToolDef(
        name="web_fetch_page",
        summary="web_fetch_page",
        properties={},
        required=[],
        handler=lambda args: (fetch_calls.append(args) or "page", [], None),
    )
    search = _tool("search_tavily", metadata={"subagent_kind": "search"})
    runtime = AgentRuntime(
        llm=_FakeLLM("{}"),
        tools=(web_fetch,),
        tools_schema=(),
        runtime_config=ChatConfig(),
        subagents=SubagentSpec(
            research_enabled=True,
            research_budget=SubagentBudgetSpec(),
        ),
    )

    with patch(
        "chatcopilot.agent.runtime.build_subagent_tools",
        return_value=(search,),
    ):
        session = runtime.new_session(
            session_id="sid",
            prompt_input=prompt_input("base"),
            permission_filter=lambda tool: (
                "denied" if tool.name == "web_fetch_page" else None
            ),
        )

    result = session.tool_executor.execute(
        "search_information",
        {
            "objective": "read this page",
            "urls": ["https://example.com/page"],
            "source_hints": ["url"],
        },
    )
    payload = json.loads(result.summary)

    assert fetch_calls == []
    assert payload["plan"]["steps"][0]["source"] == "web"
