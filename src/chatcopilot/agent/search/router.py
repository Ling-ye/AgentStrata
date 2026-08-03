"""Tool-free LLM router that emits a validated :class:`SearchPlan`."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Sequence

from chatcopilot.core.config import load_llm_profile
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.agent.search.models import (
    LOGICAL_SOURCES,
    READ_STRATEGIES,
    SearchPlan,
    SearchRequest,
    SearchAction,
)
from chatcopilot.contracts.subagents import SubagentBudgetSpec

_LOGGER = logging.getLogger(__name__)
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_SIZE = 256
_VERIFY_RE = re.compile(
    r"(latest|today|current|compare|comparison|recommend|verify|cross[- ]?check|"
    r"最新|今天|当前|比较|对比|推荐|核实|验证)",
    re.IGNORECASE,
)
_MULTI_ENTITY_RE = re.compile(
    r"(?:\b(?:compare|comparison|versus|vs\.?|difference between)\b|比较|对比|区别)",
    re.IGNORECASE,
)
_SYSTEM_PROMPT = """You route information-research requests.
Return exactly one JSON object and no prose.
Choose only from the available logical sources supplied by the caller.
Logical sources:
- web: general web, news, documentation, official sites, wikis
- experience: first-hand lifestyle or community experience
- commerce: product listings, prices, specifications, shopping links
- github: repositories, source, issues, pull requests, releases
- url: read a concrete URL, preferring static/API content before browser rendering

Schema:
{
  "operation": "search|read_url|query_system|mixed",
  "steps": [{
    "source": "web|experience|commerce|github|url",
    "query": "concise source-specific query",
    "urls": ["https://..."],
    "required_fields": ["title", "url"],
    "read_strategy": "search_only|search_then_read|static_then_browser"
  }],
  "cross_check": false,
  "reason": "short routing reason"
}

Rules:
- The caller supplies max_steps; produce at most that many steps.
- Preserve every explicit source_hint when it is available.
- If the request names Xiaohongshu, XHS, or 小红书, use the experience source.
- Preserve concrete input URLs in a url step.
- Use mixed only when more than one source class is needed.
- Do not name providers such as Tavily, SearXNG, Brave, Playwright, or MCP.
- Do not make permission or availability claims.

Query decomposition (CRITICAL for thorough depth):
- When the objective contains a comparison, multiple entities, or multi-faceted question,
  decompose it into separate steps with focused sub-queries — even for the SAME source.
  Example: "Compare Unity URP vs HDRP performance" should become two web steps:
  [{"source":"web","query":"Unity URP rendering performance benchmarks"},
   {"source":"web","query":"Unity HDRP rendering performance benchmarks"}]
- For simple, single-entity questions, a single step is sufficient.
- Each sub-query must be self-contained and search-engine-friendly.

Domain-aware strategy:
- When domain is "technical": prefer official documentation sites, use "search_then_read"
  to access full page content; query should target exact API/function/class names.
- When domain is "news": add time constraints to query, prefer recent results.
- When domain is "game": prefer official wikis and community databases, then forums.
- When domain is "consumer": prefer commerce source for product info, web for reviews.
"""


class SearchRouter:
    def __init__(
        self,
        *,
        main_llm: LLMClient,
        budget: SubagentBudgetSpec,
    ) -> None:
        self._main_llm = main_llm
        self._budget = budget
        self._router_llm: LLMClient | None = None
        self._cache: OrderedDict[str, tuple[float, SearchPlan]] = OrderedDict()

    def route(
        self,
        request: SearchRequest,
        *,
        available_sources: Sequence[str],
    ) -> SearchPlan:
        available = tuple(
            item for item in LOGICAL_SOURCES if item in set(available_sources)
        )
        if not available:
            return SearchPlan(
                operation="search",
                steps=(),
                route_source="fallback",
                route_reason="no search source is available",
            )
        script_reason = _deterministic_route_reason(request)
        if script_reason:
            return dataclasses.replace(
                self._fallback(request, available, reason=script_reason),
                route_source="script",
            )
        key = self._cache_key(request, available)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        max_steps = request.max_steps
        payload = {
            "request": request.to_dict(),
            "available_sources": list(available),
            "domain": request.domain,
            "max_steps": max_steps,
        }
        try:
            result = self.resolve_llm().chat(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    },
                ],
                tools=None,
                stream=False,
                max_retries=0,
                timeout=max(1, self._budget.timeout_seconds),
            )
            raw = json.loads(_extract_json_object(result.content))
            plan = self._normalize_plan(
                raw,
                request=request,
                available=available,
                route_source="llm",
                max_steps=max_steps,
            )
            if plan.route_source == "llm":
                self._cache_set(key, plan)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("search router failed: %s", exc)
            degraded = request
            if request.depth == "thorough":
                degraded = dataclasses.replace(request, depth="standard")
            plan = self._fallback(
                degraded,
                available,
                reason=f"router failed: {type(exc).__name__}",
            )
        return plan

    def _normalize_plan(
        self,
        raw: Any,
        *,
        request: SearchRequest,
        available: tuple[str, ...],
        route_source: str,
        max_steps: int = 3,
    ) -> SearchPlan:
        if not isinstance(raw, dict):
            return self._fallback(request, available, reason="router returned non-object")
        steps: list[SearchAction] = []
        raw_steps = raw.get("steps")
        if isinstance(raw_steps, list):
            for item in raw_steps[:max_steps]:
                step = _parse_step(item, request=request, available=available)
                if step is not None:
                    steps.append(step)

        steps = _preserve_explicit_inputs(
            steps, request=request, available=available, max_steps=max_steps
        )
        if not steps:
            return self._fallback(request, available, reason="router returned no valid steps")

        steps = _cap_single_source_steps(steps, max_steps)

        operation = _operation_for_steps(steps)
        forced_cross_check = _requires_cross_check(request)
        cross_check = forced_cross_check or (
            request.verification != "none" and bool(raw.get("cross_check"))
        )
        return SearchPlan(
            operation=operation,
            steps=tuple(steps),
            cross_check=cross_check,
            route_source=route_source,
            route_reason=str(raw.get("reason") or "validated router plan").strip()[:200],
        )

    def _fallback(
        self,
        request: SearchRequest,
        available: tuple[str, ...],
        *,
        reason: str,
    ) -> SearchPlan:
        max_steps = request.max_steps
        sources: list[str] = []
        if request.urls and "url" in available:
            sources.append("url")
        sources.extend(
            source
            for source in request.source_hints
            if source in available and source not in sources
        )
        if not sources:
            sources.append("web" if "web" in available else available[0])
        steps = tuple(
            SearchAction(
                source=source,
                query=request.objective,
                urls=request.urls if source == "url" else (),
                required_fields=request.required_fields,
                read_strategy=(
                    "static_then_browser" if source == "url" else "search_then_read"
                ),
            )
            for source in sources[:max_steps]
        )
        return SearchPlan(
            operation=_operation_for_steps(list(steps)),
            steps=steps,
            cross_check=_requires_cross_check(request),
            route_source="fallback",
            route_reason=reason[:200],
        )

    def resolve_llm(self) -> LLMClient:
        prefix = self._budget.model_env_prefix
        if not prefix:
            return self._main_llm
        if self._router_llm is None:
            fallback = self._main_llm.config
            profile = load_llm_profile(prefix, fallback=fallback)
            if profile == fallback:
                return self._main_llm
            self._router_llm = LLMClient(profile)
        return self._router_llm

    def _cache_key(
        self, request: SearchRequest, available: tuple[str, ...]
    ) -> str:
        payload = json.dumps(
            {"request": request.to_dict(), "available": list(available)},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> SearchPlan | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        created, plan = entry
        if time.monotonic() - created >= _CACHE_TTL_SECONDS:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return plan

    def _cache_set(self, key: str, plan: SearchPlan) -> None:
        self._cache[key] = (time.monotonic(), plan)
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_MAX_SIZE:
            self._cache.popitem(last=False)


_SINGLE_SOURCE_MAX_STEPS = 2


def _deterministic_route_reason(request: SearchRequest) -> str:
    if request.urls:
        return "explicit URL input"
    if request.source_hints:
        return "explicit logical source input"
    if request.depth == "quick":
        return "quick depth"
    if request.depth != "thorough":
        return "standard single-source default"
    if not _MULTI_ENTITY_RE.search(request.objective):
        return "thorough single-entity request"
    return ""


def _cap_single_source_steps(
    steps: list[SearchAction], max_steps: int
) -> list[SearchAction]:
    """Limit total steps and cap same-source decomposition.

    When every step targets the same logical source, extra queries have
    high overlap and diminishing returns while each one consumes a full
    subagent budget.  Cap to ``_SINGLE_SOURCE_MAX_STEPS`` in that case.
    """
    steps = steps[:max_steps]
    sources = {step.source for step in steps}
    if len(sources) == 1 and len(steps) > _SINGLE_SOURCE_MAX_STEPS:
        steps = steps[:_SINGLE_SOURCE_MAX_STEPS]
    return steps


def _parse_step(
    raw: Any,
    *,
    request: SearchRequest,
    available: tuple[str, ...],
) -> SearchAction | None:
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("source") or "").strip()
    if source not in available:
        return None
    urls = request.urls if source == "url" else ()
    if source == "url" and not urls:
        return None
    strategy = str(raw.get("read_strategy") or "").strip()
    if strategy not in READ_STRATEGIES:
        strategy = "static_then_browser" if source == "url" else "search_then_read"
    fields = _string_tuple(raw.get("required_fields")) or request.required_fields
    return SearchAction(
        source=source,
        query=str(raw.get("query") or request.objective).strip() or request.objective,
        urls=urls,
        required_fields=fields,
        read_strategy=strategy,
    )


def _preserve_explicit_inputs(
    steps: list[SearchAction],
    *,
    request: SearchRequest,
    available: tuple[str, ...],
    max_steps: int = 3,
) -> list[SearchAction]:
    required_sources: list[str] = []
    if request.urls and "url" in available:
        required_sources.append("url")
    required_sources.extend(
        source
        for source in request.source_hints
        if source in available and source not in required_sources
    )
    if request.source_hints:
        allowed = set(required_sources)
        steps = [step for step in steps if step.source in allowed]
    existing = {step.source for step in steps}
    missing = [source for source in required_sources if source not in existing]
    prefix = [
        SearchAction(
            source=source,
            query=request.objective,
            urls=request.urls if source == "url" else (),
            required_fields=request.required_fields,
            read_strategy="static_then_browser" if source == "url" else "search_then_read",
        )
        for source in missing
    ]
    return [*prefix, *steps][:max_steps]


def _requires_cross_check(request: SearchRequest) -> bool:
    if request.verification == "none":
        return False
    if request.verification == "required":
        return True
    return bool(_VERIFY_RE.search(request.objective))


def _operation_for_steps(steps: list[SearchAction]) -> str:
    sources = {step.source for step in steps}
    if len(sources) > 1:
        return "mixed"
    if sources == {"url"}:
        return "read_url"
    if sources == {"github"}:
        return "query_system"
    return "search"


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _extract_json_object(text: str) -> str:
    content = str(text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("router returned no JSON object")
    return content[start : end + 1]


ResearchRouter = SearchRouter

__all__ = ["SearchRouter", "ResearchRouter"]
