"""Coordinated search execution with reflection and fallback decisions."""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from chatcopilot.agent.search.reranker import ResultReranker, prepare_results
from chatcopilot.agent.search.models import SearchAction, SearchRequest
from chatcopilot.agent.search.page_reader import PageReader
from chatcopilot.agent.search.providers import (
    DIRECT_SEARCH_SERVERS,
    DirectSearchProvider,
    SearchProviderRegistry,
)
from chatcopilot.agent.search.router import SearchRouter
from chatcopilot.agent.search.results import (
    _actual_source,
    _base_actual_sources,
    _compact_results,
    _failed,
    _reflect_result,
    _reflect_results,
    _successful_actual_sources,
    _summary_for,
)
from chatcopilot.agent.trace import TraceContext, current_trace, reset_trace, set_trace
from chatcopilot.agent.turn_support import safe_emit
from chatcopilot.contracts.agent import AgentEvent, SpanFinished
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult

_MAX_PAGE_SUMMARY_CHARS = 12000
_MAX_URLS_PER_STEP = 5
_MAX_DEEP_READ_URLS = 2
_MAX_BUFFERED_STEP_EVENTS = 1024
_MAX_OMITTED_EVENT_COUNT = (1 << 63) - 1


class SearchCoordinator:
    def __init__(
        self,
        *,
        router: SearchRouter,
        registry: SearchProviderRegistry,
        provider: DirectSearchProvider,
        page_reader: PageReader,
        reranker: ResultReranker | None = None,
        max_wall_seconds: float | None = None,
    ) -> None:
        self._router = router
        self._registry = registry
        self._provider = provider
        self._page_reader = page_reader
        self._reranker = reranker
        self._max_wall = max_wall_seconds

    def run(self, request: SearchRequest) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + self._max_wall if self._max_wall else None
        available = self._registry.available_sources()
        plan = self._router.route(request, available_sources=available)
        if (
            plan.route_source == "fallback"
            and "router failed" in plan.route_reason
            and request.depth == "thorough"
        ):
            request = dataclasses.replace(request, depth="standard")

        results = self._execute_steps(
            plan.steps,
            request=request,
            cross_check=plan.cross_check,
            deadline=deadline,
        )
        actual_sources = _successful_actual_sources(results)
        if (
            (deadline is None or time.monotonic() < deadline)
            and plan.cross_check
            and len(actual_sources) < 2
            and plan.steps
        ):
            extra = self._cross_check(
                plan.steps[0],
                request=request,
                excluded_sources=set(actual_sources),
            )
            if extra is not None:
                results.append(extra)

        results, result_processing = prepare_results(results)

        ok_results = [item for item in results if item.get("ok")]
        actual_sources = _successful_actual_sources(results)
        cross_check_completed = not plan.cross_check or len(actual_sources) >= 2
        completed = bool(ok_results) and cross_check_completed
        reflection = _reflect_results(results)
        if ok_results and not completed:
            reflection["status"] = "partial_enough"

        reranked = None
        if (
            self._reranker is not None
            and (deadline is None or time.monotonic() < deadline)
            and self._reranker.should_rerank(request.depth, ok_results)
        ):
            reranked = self._reranker.rerank(request.objective, ok_results)

        output: dict[str, Any] = {
            "ok": completed,
            "summary": _summary_for(completed, ok_results, results, reflection),
            "plan": plan.to_dict(),
            "results": _compact_results(results),
            "actual_sources": actual_sources,
            "reflection": reflection,
            "result_processing": result_processing,
            "limits": {
                "depth": request.depth,
                "max_steps": request.max_steps,
                "cross_check_requested": plan.cross_check,
                "cross_check_completed": cross_check_completed,
                "partial": bool(ok_results) and not completed,
            },
        }
        if reranked is not None:
            output["reranked"] = reranked
        return output

    def _execute_steps(
        self,
        steps: tuple[SearchAction, ...],
        *,
        request: SearchRequest,
        cross_check: bool,
        deadline: float | None,
    ) -> list[dict[str, Any]]:
        if len(steps) <= 1:
            return [
                self._execute_with_reflection(step, request=request, cross_check=cross_check)
                for step in steps
            ]
        results: list[dict[str, Any]] = [{} for _ in steps]
        event_batches: list[tuple[AgentEvent, ...]] = [() for _ in steps]
        dropped_event_counts = [0 for _ in steps]
        parent_trace = current_trace()
        replay_sink = parent_trace.sink if parent_trace is not None else None

        def execute_step(
            step: SearchAction,
        ) -> tuple[dict[str, Any], tuple[AgentEvent, ...], int]:
            buffered_events: list[AgentEvent] = []
            dropped_events = 0

            def collect_event(event: AgentEvent) -> None:
                nonlocal dropped_events
                if len(buffered_events) >= _MAX_BUFFERED_STEP_EVENTS:
                    dropped_events = min(_MAX_OMITTED_EVENT_COUNT, dropped_events + 1)
                    return
                buffered_events.append(event)

            trace_token = None
            if parent_trace is not None:
                trace_token = set_trace(
                    TraceContext(
                        trace_id=parent_trace.trace_id,
                        span_id=parent_trace.span_id,
                        depth=parent_trace.depth,
                        sink=collect_event if replay_sink is not None else None,
                    )
                )
            try:
                result = self._execute_with_reflection(
                    step,
                    request=request,
                    cross_check=cross_check,
                )
            except Exception as exc:  # noqa: BLE001
                result = _failed(step.source, f"search_step_error: {exc}")
            finally:
                if trace_token is not None:
                    reset_trace(trace_token)
            return result, tuple(buffered_events), dropped_events

        remaining = deadline - time.monotonic() if deadline is not None else None
        step_contexts = [contextvars.copy_context() for _ in steps]
        with ThreadPoolExecutor(max_workers=min(3, len(steps))) as pool:
            futures = {
                pool.submit(step_contexts[idx].run, execute_step, step): idx
                for idx, step in enumerate(steps)
            }
            try:
                for future in as_completed(futures, timeout=remaining):
                    idx = futures[future]
                    try:
                        (
                            results[idx],
                            event_batches[idx],
                            dropped_event_counts[idx],
                        ) = future.result()
                    except Exception as exc:  # noqa: BLE001
                        results[idx] = _failed(steps[idx].source, f"search_step_error: {exc}")
            except TimeoutError:
                for future, idx in futures.items():
                    if not future.done():
                        results[idx] = _failed(steps[idx].source, "time_budget_exhausted")
        for future, idx in futures.items():
            if event_batches[idx] or not future.done():
                continue
            try:
                _, event_batches[idx], dropped_event_counts[idx] = future.result()
            except Exception:  # noqa: BLE001 - result already carries the step failure
                continue
        if replay_sink is not None and parent_trace is not None:
            for idx, event_batch in enumerate(event_batches):
                for event in event_batch:
                    safe_emit(replay_sink, event)
                omitted_count = dropped_event_counts[idx]
                if omitted_count:
                    omission_key = (
                        f"{parent_trace.trace_id}\0{parent_trace.span_id}\0{idx}\0"
                        "search-step-event-buffer-limit"
                    )
                    safe_emit(
                        replay_sink,
                        SpanFinished(
                            name="Search step telemetry omitted by buffer limit",
                            kind="provider_omission",
                            ok=False,
                            summary=(
                                "Additional nested search events exceeded the bounded "
                                "worker telemetry buffer."
                            ),
                            trace_id=parent_trace.trace_id,
                            span_id=(
                                "span_"
                                + hashlib.sha256(omission_key.encode("utf-8")).hexdigest()[:12]
                            ),
                            parent_span_id=parent_trace.span_id,
                            depth=parent_trace.depth + 1,
                            data={
                                "status": "truncated",
                                "reason": "search_step_event_buffer_limit",
                                "omitted_count": omitted_count,
                                "projected_event_limit": _MAX_BUFFERED_STEP_EVENTS,
                                "step_index": idx,
                                "source": steps[idx].source,
                            },
                        ),
                    )
        return results

    def _execute_with_reflection(
        self,
        step: SearchAction,
        *,
        request: SearchRequest,
        cross_check: bool,
    ) -> dict[str, Any]:
        first = self._execute_step(step, request=request, cross_check=cross_check)
        decision = _reflect_result(first)
        if decision not in {"irrelevant", "tool_error", "timeout"}:
            first["reflection"] = {"status": decision}
            return first
        retry = self._retry_step(step, request=request, first=first, decision=decision)
        if retry is None:
            first["reflection"] = {"status": decision}
            return first
        retry["reflection"] = {
            "status": _reflect_result(retry),
            "retried_after": decision,
        }
        return retry

    def _execute_step(
        self,
        step: SearchAction,
        *,
        request: SearchRequest,
        cross_check: bool,
    ) -> dict[str, Any]:
        if step.source == "url":
            return self._read_urls(step, request=request)
        if step.source in DIRECT_SEARCH_SERVERS:
            result = self._provider.search(
                logical_source=step.source,
                query=step.query or request.objective,
            )
            if result is not None:
                return self._deep_read_search_result(result, step=step, request=request)
        tool = self._registry.delegate_for_source(step.source)
        if tool is None:
            return _failed(step.source, "source_unavailable")
        return _invoke(
            tool,
            _delegate_args(step, request=request, cross_check=cross_check),
            logical_source=step.source,
        )

    def _retry_step(
        self,
        step: SearchAction,
        *,
        request: SearchRequest,
        first: dict[str, Any],
        decision: str,
    ) -> dict[str, Any] | None:
        if step.source != "web":
            return None
        excluded = {
            str(first.get("actual_source") or "")
        } if first.get("actual_source") else set()
        retry_query = _rewrite_query(step.query or request.objective)
        direct = self._provider.search(
            logical_source="web",
            query=retry_query,
            exclude_servers=excluded,
        )
        if direct is not None:
            direct.setdefault("retry", {})["reason"] = decision
            return self._deep_read_search_result(direct, step=step, request=request)
        delegate = self._registry.secondary_web_delegate(excluded)
        if delegate is None:
            return None
        return _invoke(
            delegate,
            _delegate_args(
                dataclasses.replace(step, query=retry_query),
                request=request,
                cross_check=False,
            ),
            logical_source="web",
        )

    def _read_urls(self, step: SearchAction, *, request: SearchRequest) -> dict[str, Any]:
        urls = step.urls or request.urls
        if not urls:
            return _failed("url", "url_missing")
        pages = self._page_reader.read_many(
            urls,
            objective=step.query or request.objective,
            required_fields=step.required_fields or request.required_fields,
            max_urls=_MAX_URLS_PER_STEP,
            allow_dynamic=True,
        )
        return {
            "ok": any(item.get("ok") for item in pages),
            "logical_source": "url",
            "actual_source": "url",
            "pages": pages,
        }

    def _deep_read_search_result(
        self,
        result: dict[str, Any],
        *,
        step: SearchAction,
        request: SearchRequest,
    ) -> dict[str, Any]:
        if not result.get("ok") or step.read_strategy != "search_then_read":
            return result
        summary = result.get("summary")
        if not isinstance(summary, dict):
            return result
        items = summary.get("items")
        if not isinstance(items, list) or not self._page_reader.available:
            return result
        urls = [str(item.get("url") or "") for item in items if isinstance(item, dict)]
        fetched_pages = self._page_reader.read_many(
            urls,
            objective=step.query or request.objective,
            required_fields=step.required_fields or request.required_fields,
            max_urls=_MAX_DEEP_READ_URLS,
            allow_dynamic=True,
        )
        result = dict(result)
        result["summary"] = {**summary, "fetched_pages": fetched_pages}
        return result

    def _cross_check(
        self,
        original: SearchAction,
        *,
        request: SearchRequest,
        excluded_sources: set[str],
    ) -> dict[str, Any] | None:
        cross_source = original.source if original.source != "url" else "web"
        excluded_providers = _base_actual_sources(excluded_sources)
        cross_step = dataclasses.replace(
            original,
            source=cross_source,
            read_strategy="search_only",
        )
        if cross_source in DIRECT_SEARCH_SERVERS:
            result = self._provider.search(
                logical_source=cross_source,
                query=cross_step.query or request.objective,
                exclude_servers=excluded_providers,
            )
            if result is not None and result.get("ok"):
                return result
        delegate = (
            self._registry.secondary_web_delegate(excluded_providers)
            if cross_source == "web"
            else self._registry.delegate_for_source("web")
        )
        if delegate is None:
            return None
        return _invoke(
            delegate,
            _delegate_args(cross_step, request=request, cross_check=False),
            logical_source=cross_source,
        )


def _delegate_args(
    step: SearchAction,
    *,
    request: SearchRequest,
    cross_check: bool,
) -> dict[str, Any]:
    objective = step.query or request.objective
    if step.source == "github":
        return {
            "objective": objective,
            "deliverable": "Structured GitHub evidence for the main agent.",
            "constraints": [f"time_window={request.time_window}"],
            "acceptance_criteria": list(step.required_fields or request.required_fields),
        }
    domain = {
        "experience": "consumer",
        "commerce": "consumer",
        "web": _infer_web_domain(objective, request.domain),
    }.get(step.source, "general")
    args: dict[str, Any] = {
        "objective": objective,
        "deliverable": "Structured source evidence for the main agent.",
        "domain": domain,
        "depth": request.depth,
        "target_sites": [],
        "time_window": request.time_window,
        "required_fields": list(step.required_fields or request.required_fields),
        "cross_check": cross_check,
    }
    if domain == "technical":
        args["constraints"] = [
            "prefer official documentation and API references",
            "use search_then_read for full page content when snippet is insufficient",
            "include version numbers and compatibility info when available",
        ]
    elif domain == "news":
        args["constraints"] = [
            "prefer most recent results",
            "include publication date for every result",
        ]
    return args


def _infer_web_domain(text: str, request_domain: str = "general") -> str:
    if request_domain and request_domain != "general":
        return request_domain
    lowered = text.casefold()
    if any(token in lowered for token in (
        "github", "api", "python", "unity", "代码", "技术", "文档",
        "documentation", "sdk", "library", "framework", "package",
        "npm", "pip", "cargo", "maven", "module", "函数", "类",
        "接口", "配置", "部署", "docker", "kubernetes", "rust",
        "typescript", "golang", "java", "kotlin", "swift",
    )):
        return "technical"
    if any(token in lowered for token in (
        "news", "today", "latest", "yesterday", "新闻", "今天", "最新",
        "昨天", "刚刚", "breaking", "发布", "announced", "released",
    )):
        return "news"
    if any(token in lowered for token in (
        "game", "tarkov", "boss", "游戏", "塔科夫", "原神", "崩坏",
        "steam", "xbox", "playstation", "nintendo", "switch",
        "valorant", "league", "dota", "overwatch",
    )):
        return "game"
    if any(token in lowered for token in (
        "price", "product", "buy", "商品", "价格", "购买", "推荐",
        "性价比", "coupon", "discount", "优惠", "评测", "开箱",
    )):
        return "consumer"
    return "general"


def _invoke(
    tool: ToolDef,
    args: dict[str, Any],
    *,
    logical_source: str,
) -> dict[str, Any]:
    try:
        result = tool.handler(args, ToolContext())
    except Exception as exc:  # noqa: BLE001
        return _failed(logical_source, f"{type(exc).__name__}: {exc}", actual_source=tool.name)
    if not isinstance(result, ToolResult):
        return _failed(logical_source, "invalid_tool_result", actual_source=tool.name)
    payload = dict(result.data) or _parse_payload(result.summary)
    ok = result.ok and payload.get("ok") is not False
    return {
        "ok": ok,
        "logical_source": logical_source,
        "actual_source": _actual_source(tool.name, payload),
        "summary": payload if payload else (result.summary or result.error or ""),
        "outputs": list(result.outputs),
    }


def _parse_payload(summary: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(summary))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}










def _rewrite_query(query: str) -> str:
    text = str(query or "").strip()
    if '"' in text:
        return text
    return f'"{text}" official source latest'.strip()














__all__ = ["SearchCoordinator"]
