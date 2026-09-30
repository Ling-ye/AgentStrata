from __future__ import annotations

import json
from dataclasses import replace
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
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema


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
    def handler(args: dict, _context: ToolContext) -> ToolResult:
        calls.append(f"{server_id}:{args['query']}")
        if payload.get("ok") is False:
            return ToolResult(
                ok=False,
                error=str(payload.get("error") or "search provider failed"),
                error_code=str(payload.get("error_code") or "mcp_unavailable"),
                data=dict(payload),
            )
        return ToolResult(
            ok=True,
            summary=json.dumps(payload, ensure_ascii=False),
            data={"content": dict(payload)},
        )

    remote = "brave_web_search" if server_id == "brave" else "search"
    return ToolDef(
        name=f"raw_{server_id}",
        summary=f"raw_{server_id}",
        input_schema=object_schema(
            {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            required=("query",),
        ),
        output_schema=object_schema(
            {"content": {"type": "object"}},
            required=("content",),
        ),
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
    def handler(args: dict, _context: ToolContext) -> ToolResult:
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
        return ToolResult(
            ok=True,
            summary=json.dumps(wrapper_payload, ensure_ascii=False),
            data={"content": wrapper_payload},
        )

    return ToolDef(
        name="xhs_search_feeds",
        summary="xhs_search_feeds",
        input_schema=object_schema(
            {"keyword": {"type": "string"}, "limit": {"type": "integer"}},
            required=("keyword",),
        ),
        output_schema=object_schema(
            {"content": {"type": "object"}},
            required=("content",),
        ),
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
    def handler(_args: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, summary=summary, data={})

    return ToolDef(
        name=name,
        summary=name,
        input_schema=object_schema(additional_properties=True),
        output_schema=object_schema(),
        handler=handler,
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
    ("turn_timeout", "budget_timeout", "expected_wall"),
    [(100.0, 120, 100.0), (1000.0, 120, 120.0), (None, 120, 120.0),
     (1000.0, 600, 600.0), (100.0, 600, 100.0)],
)
def test_search_wall_budget_intersects_declared_and_parent_budget(
    monkeypatch: pytest.MonkeyPatch,
    turn_timeout: float | None,
    budget_timeout: int,
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
        budget=SubagentBudgetSpec(timeout_seconds=budget_timeout),
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
                    runtime_id=f"search-step-{step.source}",
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
                    runtime_id=f"search-step-{step.source}",
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
    assert [event.runtime_id for event in replayed] == [
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
                        runtime_id="overflow-search-step",
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
        if isinstance(event, LlmCallStarted) and event.runtime_id == "overflow-search-step"
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

    result = ToolExecutor(caller_role_hint="owner", tools=[search]).execute(
        "search_information",
        {"objective": "package release", "verification": "none"},
    )
    payload = result.data

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

    result = ToolExecutor(caller_role_hint="owner", tools=[search]).execute(
        "search_information",
        {"objective": "Unity package release", "verification": "none"},
    )
    payload = result.data
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

    result = ToolExecutor(caller_role_hint="owner", tools=[search]).execute(
        "search_information",
        {"objective": "上海 二郎拉面 探店", "verification": "none"},
    )
    payload = result.data
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
    result = ToolExecutor(caller_role_hint="owner", tools=[search]).execute(
        "search_information",
        {"objective": objective, "verification": "none"},
    )
    payload = result.data

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

    result = ToolExecutor(caller_role_hint="owner", tools=[search]).execute(
        "search_information",
        {"objective": "Cultist warrior health", "verification": "none"},
    )
    payload = result.data
    fetched = payload["results"][0]["summary"]["fetched_pages"]

    assert fetched[0]["method"] == "dynamic"
    assert fetched[0]["actual_source"] == "playwright"


def test_compaction_uses_the_request_budget_for_counts_and_text():
    from chatcopilot.agent.search.models import SearchBudget
    from chatcopilot.agent.search.results import _compact_results

    results = [{"ok": True, "summary": {"items": list(range(8))}}]
    compacted = _compact_results(results, budget=SearchBudget(3, max_result_items=4))
    assert compacted[0]["summary"]["items"] == [0, 1, 2, 3]
    assert compacted[0]["summary"]["items_total"] == 8
    text = _compact_results([{"summary": "x" * 500}], budget=SearchBudget(3, max_result_chars=100))
    assert text[0]["truncated"] is True
    assert len(text[0]["summary"]) < 150
    pages = [{"ok": True, "pages": [{"summary": '中文\\"\n' * 1000}]}]
    preview = _compact_results(pages, budget=SearchBudget(3, max_result_chars=200))
    assert preview[0]["truncated"] is True
    assert len(json.dumps(preview, ensure_ascii=False)) <= 200
    assert len(pages[0]["pages"][0]["summary"]) > 200


def test_configured_url_batch_reaches_actual_page_reads():
    from chatcopilot.contracts.subagents import SearchLimitsSpec
    calls = []
    page = _tool("web_fetch_page", "fixture page")
    original = page.handler
    def read(args, ctx):
        calls.append(args["url"])
        return original(args, ctx)
    page = replace(page, handler=read)
    search = build_search_tool(main_llm=_router_for_web(), budget=SubagentBudgetSpec(timeout_seconds=600),
        tools=(page,), limits=SearchLimitsSpec(max_urls=25))
    assert search is not None
    urls = [f"https://example.com/{i}" for i in range(21)]
    result = search.handler({"objective": "read all pages", "urls": urls, "verification": "none"}, ToolContext())
    assert result.ok and calls == urls
    assert len(result.data["full_results"][0]["pages"]) == 21
    rejected = search.handler({"objective": "read all pages", "urls": urls + [f"https://other.example/{i}" for i in range(5)]}, ToolContext())
    assert not rejected.ok and len(calls) == 21


def test_collected_search_results_after_preview_are_readable():
    from chatcopilot.agent.tools.result_reader import SessionResultStore
    entries = [{"title": f"package result {i}", "url": f"https://example.com/{i}",
                "content": f"package EVIDENCE-{i}- " + "x" * 1500} for i in range(30)]
    provider = _raw_search("tavily", {"results": entries}, [])
    search = build_search_tool(main_llm=_router_for_web(), budget=SubagentBudgetSpec(),
                               tools=(), raw_mcp_tools=(provider,))
    assert search is not None
    store = SessionResultStore()
    executor = ToolExecutor(caller_role_hint="owner", tools=[search], result_store=store)
    result = executor.execute("search_information", {"objective": "package release", "verification": "none"})
    assert result.ok
    assert len(result.data["full_results"][0]["summary"]["items"]) == 30
    projected = executor.project_result(search.name, {"ok": True, "summary": result.summary, "data": result.data})
    assert "full_results" not in projected["data"]
    assert len(projected["data"]["results"][0]["summary"]["items"]) <= 15
    read = store.read({"result_id": projected["result_ref"]["id"], "query": "EVIDENCE-29-"}, ToolContext(caller_role="owner"))
    assert read.ok and read.data["found"] and "EVIDENCE-29-" in read.data["text"]
    executor.close()


def test_full_fetched_page_tail_survives_search_preview_and_result_readback(monkeypatch):
    from chatcopilot.agent.tools.result_reader import SessionResultStore
    from chatcopilot.external_tools.web_fetch import tools as fetch
    from tests.unit.test_web_fetch import _FakeResponse

    body = ("<p>" + "正文" * 20000 + "TAIL-EVIDENCE</p>").encode()
    monkeypatch.setattr(fetch.urllib.request, "urlopen",
                        lambda *args, **kwargs: _FakeResponse(body, "text/html; charset=utf-8"))
    search = build_search_tool(main_llm=_router_for_web(), budget=SubagentBudgetSpec(),
                               tools=(fetch.web_fetch_page,))
    assert search is not None
    store = SessionResultStore()
    executor = ToolExecutor(caller_role_hint="owner", tools=[search], result_store=store)
    try:
        result = executor.execute(search.name, {"objective": "read all", "urls": ["https://example.com"],
                                                "verification": "none"})
        assert result.ok and result.data["ok"]
        page = result.data["full_results"][0]["pages"][0]
        assert "TAIL-EVIDENCE" not in page["summary"]
        assert "TAIL-EVIDENCE" in page["content"] and page["complete"]
        projected = executor.project_result(search.name, result.to_llm_payload())
        read = store.read({"result_id": projected["result_ref"]["id"], "query": "TAIL-EVIDENCE"},
                          ToolContext(caller_role="owner"))
        assert read.ok and read.data["found"] and "TAIL-EVIDENCE" in read.data["text"]
    finally:
        executor.close()


@pytest.mark.parametrize("mode", ["failed", "incomplete", "complete"])
def test_url_completion_requires_every_requested_page(mode):
    def read(args, _ctx):
        affected = args["url"].endswith("/2") and mode != "complete"
        return ToolResult(ok=not (affected and mode == "failed"), summary="page content",
                          error="fixture unavailable" if affected and mode == "failed" else "",
                          data={"content": "full page", "complete": not affected})
    page = replace(_tool("web_fetch_page", "page"), handler=read)
    search = build_search_tool(main_llm=_router_for_web(), budget=SubagentBudgetSpec(), tools=(page,))
    urls = [f"https://example.com/{i}" for i in range(3)]
    result = search.handler({"objective": "read all", "urls": urls, "verification": "none"}, ToolContext())
    assert result.ok  # usable evidence is still delivered
    assert result.data["ok"] is (mode == "complete")
    assert result.data["limits"]["partial"] is (mode != "complete")
    assert result.data["limits"]["unread_requested_urls"] == (0 if mode == "complete" else 1)
    assert result.data["full_results"][0]["completed_pages"] == (3 if mode == "complete" else 2)


def test_missing_url_reader_cannot_claim_explicit_urls_completed():
    provider = _raw_search("tavily", {"results": [{"title": "package", "url": "https://example.com",
                                                 "content": "package evidence"}]}, [])
    search = build_search_tool(main_llm=_router_for_web(), budget=SubagentBudgetSpec(),
                               tools=(), raw_mcp_tools=(provider,))
    result = search.handler({"objective": "package", "urls": ["https://example.com"],
                             "verification": "none"}, ToolContext())
    assert result.data["ok"] is False
    assert result.data["limits"]["unread_requested_urls"] == 1


def test_thorough_limits_reach_router_and_deep_reader():
    from chatcopilot.contracts.subagents import SearchLimitsSpec
    from chatcopilot.agent.search.router import SearchRouter
    limits = SearchLimitsSpec(thorough_max_steps=10, thorough_max_deep_read_urls=8)
    request = SearchRequest.from_args({"objective": "compare A versus B", "depth": "thorough", "verification": "none"}, limits=limits)
    llm = _FakeLLM(json.dumps({"operation": "search", "steps": [
        {"source": "web", "query": f"package {i}", "read_strategy": "search_only"} for i in range(10)
    ], "cross_check": False}))
    plan = SearchRouter(main_llm=llm, budget=SubagentBudgetSpec(timeout_seconds=600)).route(request, available_sources=("web",))
    assert len(plan.steps) == 10
    assert json.loads(llm.calls[0]["messages"][1]["content"])["max_steps"] == 10
    pages = []
    page = _tool("web_fetch_page", "fixture page")
    original = page.handler
    def read(args, ctx):
        pages.append(args["url"])
        return original(args, ctx)
    page = replace(page, handler=read)
    provider = _raw_search("tavily", {"results": [
        {"title": f"package {i}", "url": f"https://example.com/{i}", "content": "package reference"} for i in range(10)
    ]}, [])
    search = build_search_tool(main_llm=_router_for_web(), budget=SubagentBudgetSpec(timeout_seconds=600),
                               tools=(page,), raw_mcp_tools=(provider,), limits=limits)
    result = search.handler({"objective": "package reference", "depth": "thorough", "verification": "none"}, ToolContext())
    assert result.ok and len(pages) == 8
