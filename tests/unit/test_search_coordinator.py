from __future__ import annotations

import json

from chatcopilot.core.llm_client import ChatResult
from chatcopilot.agent.search.tool import build_search_tool
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import SubagentBudgetSpec
from chatcopilot.external_tools.shared.tool_spec import ToolDef


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []
        self.model = "fake-search-router"

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return ChatResult(content=self.content, finish_reason="stop")


def _raw_search(
    server_id: str,
    payload: dict,
    calls: list[str],
) -> ToolDef:
    def handler(args: dict):
        calls.append(f"{server_id}:{args['query']}")
        return json.dumps(payload, ensure_ascii=False), [], None

    remote = "brave_web_search" if server_id == "brave" else "search"
    return ToolDef(
        name=f"raw_{server_id}",
        summary=f"raw_{server_id}",
        properties={"query": {"type": "string"}, "max_results": {"type": "integer"}},
        required=["query"],
        handler=handler,
        category="mcp",
        metadata={
            "mcp_risk": "search",
            "mcp_server_id": server_id,
            "mcp_remote_name": remote,
            "mcp_search_only_tools": [remote],
        },
    )


def _raw_xiaohongshu_search(calls: list[dict]) -> ToolDef:
    def handler(args: dict):
        calls.append(dict(args))
        text_payload = {
            "items": [
                {
                    "id": "abc123",
                    "note_card": {
                        "display_title": "上海二郎拉面探店",
                        "desc": "上海市区二郎系拉面评价不错",
                    },
                    "xsec_token": "token",
                }
            ]
        }
        wrapper_payload = {
            "is_error": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(text_payload, ensure_ascii=False),
                }
            ],
        }
        return json.dumps(wrapper_payload, ensure_ascii=False), [], None

    return ToolDef(
        name="xhs_search_feeds",
        summary="xhs_search_feeds",
        properties={"keyword": {"type": "string"}, "limit": {"type": "integer"}},
        required=["keyword"],
        handler=handler,
        category="mcp",
        metadata={
            "mcp_risk": "search",
            "mcp_server_id": "xiaohongshu",
            "mcp_remote_name": "search_feeds",
            "mcp_search_only_tools": ["search_feeds"],
        },
    )


def _tool(name: str, summary: str) -> ToolDef:
    return ToolDef(
        name=name,
        summary=name,
        properties={},
        required=[],
        handler=lambda _args: (summary, [], None),
    )


def _router_for_web(query: str = "package release") -> _FakeLLM:
    return _FakeLLM(
        json.dumps(
            {
                "operation": "search",
                "steps": [
                    {
                        "source": "web",
                        "query": query,
                        "required_fields": ["title", "url"],
                        "read_strategy": "search_only",
                    }
                ],
                "cross_check": False,
            }
        )
    )


def test_search_information_skips_tavily_quota_and_uses_brave() -> None:
    calls: list[str] = []
    tavily = _raw_search(
        "tavily",
        {"ok": False, "error_code": "mcp_quota_exceeded"},
        calls,
    )
    brave = _raw_search(
        "brave",
        {
            "results": [
                {
                    "title": "Package release notes",
                    "url": "https://example.com/release",
                    "content": "package release details",
                }
            ]
        },
        calls,
    )
    search = build_search_tool(
        main_llm=_router_for_web(),
        budget=SubagentBudgetSpec(),
        tools=(),
        raw_mcp_tools=(tavily, brave),
        circuit=SearchCircuitBreaker(),
    )
    assert search is not None

    result = ToolExecutor(tools=[search]).execute(
        "search_information",
        {"objective": "package release", "verification": "none"},
    )
    payload = json.loads(result.summary)

    assert payload["ok"] is True
    assert payload["actual_sources"] == ["brave"]
    assert calls[0].startswith("tavily:")
    assert calls[1].startswith("brave:")


def test_searxng_results_are_filtered_for_relevance() -> None:
    calls: list[str] = []
    searxng = _raw_search(
        "searxng",
        {
            "results": [
                {
                    "title": "Sign in",
                    "url": "https://noise.example/login",
                    "content": "captcha login page",
                },
                {
                    "title": "Unity package release notes",
                    "url": "https://docs.unity3d.com/release",
                    "content": "Unity package release API details",
                },
            ]
        },
        calls,
    )
    search = build_search_tool(
        main_llm=_router_for_web("Unity package release"),
        budget=SubagentBudgetSpec(),
        tools=(),
        raw_mcp_tools=(searxng,),
    )
    assert search is not None

    result = ToolExecutor(tools=[search]).execute(
        "search_information",
        {"objective": "Unity package release", "verification": "none"},
    )
    payload = json.loads(result.summary)
    items = payload["results"][0]["summary"]["items"]

    assert payload["actual_sources"] == ["searxng"]
    assert [item["url"] for item in items] == ["https://docs.unity3d.com/release"]
    assert "-site:pinterest.com" in calls[0]


def test_xiaohongshu_direct_search_uses_keyword_and_extracts_wrapped_content() -> None:
    calls: list[dict] = []
    xiaohongshu = _raw_xiaohongshu_search(calls)
    llm = _FakeLLM(
        json.dumps(
            {
                "operation": "search",
                "steps": [
                    {
                        "source": "experience",
                        "query": "上海 二郎拉面 探店",
                        "required_fields": ["title", "url"],
                        "read_strategy": "search_only",
                    }
                ],
                "cross_check": False,
            }
        )
    )
    search = build_search_tool(
        main_llm=llm,
        budget=SubagentBudgetSpec(),
        tools=(),
        raw_mcp_tools=(xiaohongshu,),
    )
    assert search is not None

    result = ToolExecutor(tools=[search]).execute(
        "search_information",
        {"objective": "上海 二郎拉面 探店", "verification": "none"},
    )
    payload = json.loads(result.summary)
    items = payload["results"][0]["summary"]["items"]

    assert calls == [{"keyword": "上海 二郎拉面 探店", "limit": 10}]
    assert payload["actual_sources"] == ["xiaohongshu"]
    assert items[0]["title"] == "上海二郎拉面探店"
    assert items[0]["url"] == "https://www.xiaohongshu.com/explore/abc123"
    assert items[0]["snippet"] == "上海市区二郎系拉面评价不错"


def test_explicit_xiaohongshu_request_forces_experience_over_web() -> None:
    xhs_calls: list[dict] = []
    web_calls: list[str] = []
    xiaohongshu = _raw_xiaohongshu_search(xhs_calls)
    searxng = _raw_search(
        "searxng",
        {
            "results": [
                {
                    "title": "Web result",
                    "url": "https://example.com/qingshan",
                    "content": "general web result",
                }
            ]
        },
        web_calls,
    )
    search = build_search_tool(
        main_llm=_router_for_web("上海 青山制面 地址 评价"),
        budget=SubagentBudgetSpec(),
        tools=(),
        raw_mcp_tools=(xiaohongshu, searxng),
    )
    assert search is not None

    objective = "使用小红书 MCP 搜索上海市的青山制面的地址和评价"
    result = ToolExecutor(tools=[search]).execute(
        "search_information",
        {"objective": objective, "verification": "none"},
    )
    payload = json.loads(result.summary)

    assert payload["actual_sources"] == ["xiaohongshu"]
    assert xhs_calls == [{"keyword": objective, "limit": 10}]
    assert web_calls == []


def test_search_result_deep_read_uses_dynamic_browser_when_static_page_is_shell() -> None:
    calls: list[str] = []
    tavily = _raw_search(
        "tavily",
        {
            "results": [
                {
                    "title": "Boss page",
                    "url": "https://tarkov.dev/boss/cultist-warrior",
                    "content": "Cultist warrior boss health",
                }
            ]
        },
        calls,
    )
    static = _tool(
        "web_fetch_page",
        "Title: Tarkov.dev\nURL: https://tarkov.dev/boss/cultist-warrior\n"
        "Content:\nPlease enable JavaScript to continue.",
    )
    dynamic = _tool(
        "browse_dynamic_page",
        json.dumps({"ok": True, "summary": "Cultist warrior health is 850"}),
    )
    llm = _FakeLLM(
        json.dumps(
            {
                "operation": "search",
                "steps": [
                    {
                        "source": "web",
                        "query": "Cultist warrior health",
                        "required_fields": ["health"],
                        "read_strategy": "search_then_read",
                    }
                ],
                "cross_check": False,
            }
        )
    )
    search = build_search_tool(
        main_llm=llm,
        budget=SubagentBudgetSpec(),
        tools=(static, dynamic),
        raw_mcp_tools=(tavily,),
    )
    assert search is not None

    result = ToolExecutor(tools=[search]).execute(
        "search_information",
        {"objective": "Cultist warrior health", "verification": "none"},
    )
    payload = json.loads(result.summary)
    fetched = payload["results"][0]["summary"]["fetched_pages"]

    assert fetched[0]["method"] == "dynamic"
    assert fetched[0]["actual_source"] == "playwright"
