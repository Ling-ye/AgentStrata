from __future__ import annotations

from unittest.mock import patch

from chatcopilot.core.config import ChatConfig
from chatcopilot.agent.context.prompt_builder import build_system_prompt
from chatcopilot.agent.runtime import AgentRuntime
from chatcopilot.agent.search_policy import (
    render_search_routing_policy,
    validate_search_task_args,
)
from chatcopilot.external_tools.shared.tool_spec import ToolDef


def test_search_policy_routes_supported_domains_to_existing_tools() -> None:
    policy = render_search_routing_policy(
        (
            "search_tavily",
            "search_searxng",
            "search_xiaohongshu",
            "search_taoke",
            "query_approved_sources",
        )
    )

    assert "`technical`" in policy
    assert "`query_approved_sources`" in policy
    assert "`game`" in policy
    assert "`consumer`" in policy
    assert "`search_taoke`" in policy
    assert "`search_xiaohongshu`" in policy
    assert "`search_tavily`" in policy
    assert "`search_brave`" not in policy
    assert "`search_searxng`" in policy


def test_search_policy_renders_unified_search_entry() -> None:
    policy = render_search_routing_policy(("search_information",))

    assert "`search_information`" in policy
    assert "统一搜索入口" in policy


def test_search_policy_is_only_injected_when_search_tools_exist() -> None:
    with_search = build_system_prompt(
        baseline="base",
        has_search_tools=True,
        search_tool_names=("search_tavily",),
    )
    without_search = build_system_prompt(baseline="base", has_search_tools=False)

    assert "## 领域搜索路由" in with_search
    assert "target_sites" in with_search
    assert "## 领域搜索路由" not in without_search


def test_search_policy_never_mentions_unavailable_tools() -> None:
    policy = render_search_routing_policy(("search_tavily", "search_xiaohongshu"))

    assert "`search_tavily`" in policy
    assert "`search_xiaohongshu`" in policy
    assert "search_searxng" not in policy
    assert "search_taoke" not in policy
    assert "query_approved_sources" not in policy


def test_runtime_routes_only_accessible_delegate_tools() -> None:
    def delegate(name: str, kind: str) -> ToolDef:
        return ToolDef(
            name=name,
            summary=name,
            properties={},
            required=[],
            handler=lambda _args: ("ok", [], None),
            metadata={"subagent_kind": kind},
        )

    runtime = AgentRuntime(
        llm=object(),
        tools=(),
        tools_schema=(),
        runtime_config=ChatConfig(),
    )
    delegates = (
        delegate("search_tavily", "search"),
        delegate("search_xiaohongshu", "search"),
        delegate("search_taoke", "search"),
    )
    with patch(
        "chatcopilot.agent.runtime.build_subagent_tools",
        return_value=delegates,
    ):
        session = runtime.new_session(
            session_id="sid",
            system_baseline="base",
            permission_filter=lambda tool: "denied" if tool.name == "search_taoke" else None,
        )

    assert "`search_tavily`" in session.system_baseline
    assert "`search_xiaohongshu`" in session.system_baseline
    assert "search_taoke" not in session.system_baseline


def test_search_task_validation_rejects_wrong_types_and_empty_items() -> None:
    errors = validate_search_task_args(
        {
            "objective": "find docs",
            "domain": 123,
            "target_sites": ["docs.example.com", ""],
            "time_window": 30,
            "required_fields": ["title", None],
            "cross_check": "false",
        }
    )

    assert "domain must be a string" in errors
    assert "target_sites must contain non-empty strings" in errors
    assert "time_window must be a string" in errors
    assert "required_fields must contain non-empty strings" in errors
    assert "cross_check must be a boolean" in errors


def test_search_task_validation_accepts_news_domain() -> None:
    errors = validate_search_task_args(
        {
            "objective": "latest release",
            "domain": "news",
            "target_sites": [],
            "time_window": "past 24 hours",
            "required_fields": ["title", "url", "published_at"],
            "cross_check": True,
        }
    )

    assert errors == ()
