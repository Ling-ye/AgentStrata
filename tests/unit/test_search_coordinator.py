from __future__ import annotations

import json
import threading
import time
from unittest import mock

import pytest

from chatcopilot.core.llm_client import ChatResult
from chatcopilot.agent.search.coordinator import SearchCoordinator
from chatcopilot.agent.search.models import SearchAction, SearchRequest
from chatcopilot.agent.search import tool as search_tool_module
from chatcopilot.agent.search.tool import build_search_tool
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.trace import TraceContext, current_trace, reset_trace, set_trace
from chatcopilot.botspec.model import SubagentBudgetSpec
from chatcopilot.contracts.agent import ContextSnapshotPrepared, LlmCallStarted, SpanFinished
from chatcopilot.contracts.tools import ToolDef


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


@pytest.mark.parametrize(
    ("turn_timeout", "expected_wall"),
    [(100.0, 60.0), (1000.0, 180.0), (None, 180.0)],
)
def test_search_wall_budget_is_sixty_percent_with_180_second_cap(
    monkeypatch: pytest.MonkeyPatch,
    turn_timeout: float | None,
    expected_wall: float,
) -> None:
    captured: list[float] = []

    class _Coordinator:
        def __init__(self, **kwargs):
            captured.append(kwargs["max_wall_seconds"])

    monkeypatch.setattr(search_tool_module, "SearchCoordinator", _Coordinator)
    raw = _raw_search("searxng", {"results": []}, [])

    search = build_search_tool(
        main_llm=_router_for_web(),
        budget=SubagentBudgetSpec(),
        tools=(),
        raw_mcp_tools=(raw,),
        turn_timeout_seconds=turn_timeout,
    )

    assert search is not None
    assert captured == [expected_wall]


def test_parallel_search_steps_replay_nested_trace_events_serially_in_plan_order() -> None:
    coordinator = object.__new__(SearchCoordinator)
    barrier = threading.Barrier(2)
    observed_contexts: list[tuple[str, object, int]] = []
    observation_lock = threading.Lock()

    def execute_step(
        step: SearchAction,
        *,
        request: SearchRequest,
        cross_check: bool,
    ) -> dict:
        del request, cross_check
        trace = current_trace()
        with observation_lock:
            observed_contexts.append((step.source, trace, threading.get_ident()))
        barrier.wait(timeout=2)
        if step.source == "web":
            time.sleep(0.05)
        if trace is not None and trace.sink is not None:
            span_id = f"span_search_{step.source}"
            snapshot_id = f"ctx_search_{step.source}"
            trace.sink(
                ContextSnapshotPrepared(
                    snapshot_id=snapshot_id,
                    backend=f"search-step-{step.source}",
                    model="nested-search-model",
                    iteration=0,
                    session_messages=(),
                    effective_messages=(),
                    trace_id=trace.trace_id,
                    span_id=span_id,
                    parent_span_id=trace.span_id,
                    depth=trace.depth + 1,
                )
            )
            trace.sink(
                LlmCallStarted(
                    model="nested-search-model",
                    iteration=0,
                    backend=f"search-step-{step.source}",
                    trace_id=trace.trace_id,
                    span_id=span_id,
                    parent_span_id=trace.span_id,
                    depth=trace.depth + 1,
                    context_snapshot_id=snapshot_id,
                )
            )
        return {"ok": True, "logical_source": step.source}

    coordinator._execute_with_reflection = execute_step
    replayed: list[object] = []
    replay_threads: list[int] = []
    caller_thread = threading.get_ident()

    def replay(event: object) -> None:
        replay_threads.append(threading.get_ident())
        replayed.append(event)

    token = set_trace(
        TraceContext(
            trace_id="trace_parallel_search",
            span_id="span_search_information",
            depth=0,
            sink=replay,
        )
    )
    try:
        results = coordinator._execute_steps(
            (
                SearchAction(source="web", query="one"),
                SearchAction(source="github", query="two"),
            ),
            request=SearchRequest(objective="compare one and two"),
            cross_check=False,
            deadline=None,
        )
    finally:
        reset_trace(token)

    assert [result["logical_source"] for result in results] == ["web", "github"]
    assert {source for source, _, _ in observed_contexts} == {"web", "github"}
    worker_contexts = [trace for _, trace, _ in observed_contexts]
    assert all(trace is not None for trace in worker_contexts)
    assert len({id(trace) for trace in worker_contexts}) == 2
    assert all(trace.trace_id == "trace_parallel_search" for trace in worker_contexts)
    assert all(trace.span_id == "span_search_information" for trace in worker_contexts)
    assert all(worker_thread != caller_thread for _, _, worker_thread in observed_contexts)
    assert replay_threads == [caller_thread] * 4
    assert [event.backend for event in replayed] == [
        "search-step-web",
        "search-step-web",
        "search-step-github",
        "search-step-github",
    ]
    assert all(event.trace_id == "trace_parallel_search" for event in replayed)
    assert all(event.parent_span_id == "span_search_information" for event in replayed)
    assert isinstance(replayed[0], ContextSnapshotPrepared)
    assert isinstance(replayed[1], LlmCallStarted)
    assert isinstance(replayed[2], ContextSnapshotPrepared)
    assert isinstance(replayed[3], LlmCallStarted)


def test_parallel_search_event_overflow_and_sink_failure_preserve_success() -> None:
    coordinator = object.__new__(SearchCoordinator)

    def execute_step(
        step: SearchAction,
        *,
        request: SearchRequest,
        cross_check: bool,
    ) -> dict:
        del request, cross_check
        trace = current_trace()
        if step.source == "web" and trace is not None and trace.sink is not None:
            for index in range(1027):
                trace.sink(
                    LlmCallStarted(
                        model="overflow-search-model",
                        iteration=index,
                        backend="overflow-search-step",
                        trace_id=trace.trace_id,
                        span_id=f"span_overflow_search_{index}",
                        parent_span_id=trace.span_id,
                        depth=trace.depth + 1,
                    )
                )
        return {"ok": True, "logical_source": step.source}

    coordinator._execute_with_reflection = execute_step
    replayed: list[object] = []
    replay_threads: list[int] = []
    sink_calls = 0
    caller_thread = threading.get_ident()

    def intermittently_failing_sink(event: object) -> None:
        nonlocal sink_calls
        sink_calls += 1
        replay_threads.append(threading.get_ident())
        replayed.append(event)
        if sink_calls == 2:
            raise RuntimeError("recorder unavailable once")

    token = set_trace(
        TraceContext(
            trace_id="trace_search_overflow",
            span_id="span_search_information",
            depth=0,
            sink=intermittently_failing_sink,
        )
    )
    try:
        with mock.patch("chatcopilot.agent.turn_support.LOGGER.exception") as logged:
            results = coordinator._execute_steps(
                (
                    SearchAction(source="web", query="overflow"),
                    SearchAction(source="github", query="control"),
                ),
                request=SearchRequest(objective="overflow telemetry without failing search"),
                cross_check=False,
                deadline=None,
            )
    finally:
        reset_trace(token)

    assert results == [
        {"ok": True, "logical_source": "web"},
        {"ok": True, "logical_source": "github"},
    ]
    logged.assert_called_once()
    retained = [
        event
        for event in replayed
        if isinstance(event, LlmCallStarted) and event.backend == "overflow-search-step"
    ]
    assert len(retained) == 1024
    omissions = [
        event
        for event in replayed
        if isinstance(event, SpanFinished)
        and event.kind == "provider_omission"
        and event.data.get("reason") == "search_step_event_buffer_limit"
    ]
    assert len(omissions) == 1
    assert omissions[0].trace_id == "trace_search_overflow"
    assert omissions[0].parent_span_id == "span_search_information"
    assert omissions[0].data.get("omitted_count") == 3
    assert omissions[0].data.get("projected_event_limit") == 1024
    assert replay_threads == [caller_thread] * 1025


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
