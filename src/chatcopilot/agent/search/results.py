"""Search result reflection, source accounting, and compaction helpers."""
from __future__ import annotations

import json
from typing import Any, Sequence

from chatcopilot.agent.search.models import SearchBudget


def _actual_source(tool_name: str, payload: dict[str, Any]) -> str:
    base = {
        "search_tavily": "tavily",
        "search_brave": "brave",
        "search_searxng": "searxng",
        "search_xiaohongshu": "xiaohongshu",
        "search_taoke": "taoke",
        "query_approved_sources": "github",
    }.get(tool_name, tool_name)
    fallback = payload.get("fallback")
    if isinstance(fallback, dict) and fallback.get("source"):
        return f"{base}:fallback_{fallback['source']}"
    return base


def _failed(
    source: str,
    error: str,
    *,
    actual_source: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "logical_source": source,
        "actual_source": actual_source or source,
        "error": error,
    }


def _reflect_result(result: dict[str, Any]) -> str:
    if result.get("ok"):
        summary = result.get("summary")
        if isinstance(summary, dict):
            items = summary.get("items")
            pages = summary.get("fetched_pages")
            if isinstance(items, list) and not items and not pages:
                return "irrelevant"
        return "hit_target"
    error = str(result.get("error") or "")
    if "timeout" in error:
        return "timeout"
    if "exhausted" in error or "unavailable" in error or "mcp_" in error:
        return "tool_error"
    return "fail"


def _reflect_results(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    statuses = [_reflect_result(item) for item in results]
    if "hit_target" in statuses:
        status = "hit_target"
    elif any(status in {"tool_error", "timeout"} for status in statuses):
        status = "tool_error"
    elif "irrelevant" in statuses:
        status = "irrelevant"
    else:
        status = "fail"
    return {"status": status, "step_statuses": statuses}


def _summary_for(
    completed: bool,
    ok_results: list[dict[str, Any]],
    results: list[dict[str, Any]],
    reflection: dict[str, Any],
) -> str:
    if completed:
        return f"search completed with {len(ok_results)}/{len(results)} successful step(s)"
    if ok_results:
        return "search returned partial evidence; not all requested verification completed"
    status = reflection.get("status")
    if status == "tool_error":
        return "search failed because all applicable tools errored or timed out"
    if status == "irrelevant":
        return "search completed but did not find relevant results"
    return "search could not find enough evidence"


def _base_actual_sources(sources: set[str]) -> set[str]:
    return {source.split(":", 1)[0] for source in sources if source}


def _successful_actual_sources(results: Sequence[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            source
            for item in results
            if item.get("ok")
            for source in [str(item.get("actual_source") or "").strip()]
            if source
        )
    )


def _compact_results(results: list[dict[str, Any]], *, budget: SearchBudget) -> list[dict[str, Any]]:
    remaining = budget.max_result_chars - 2  # JSON array brackets.
    compacted: list[dict[str, Any]] = []
    for item in results:
        separator = 2 if compacted else 0
        item = _limit_summary_items(item, max_items=budget.max_result_items)
        encoded = json.dumps(item, ensure_ascii=False)
        if len(encoded) + separator <= remaining:
            compacted.append(item)
            remaining -= len(encoded) + separator
            continue
        trimmed = _bounded_preview(item, remaining - separator)
        if trimmed is not None:
            compacted.append(trimmed)
        break
    return compacted


def _limit_summary_items(result: dict[str, Any], *, max_items: int) -> dict[str, Any]:
    summary = result.get("summary")
    if not isinstance(summary, dict):
        return result
    changed = False
    trimmed = dict(summary)
    for key in ("items", "findings", "results", "evidence", "organic_results"):
        value = trimmed.get(key)
        if isinstance(value, list) and len(value) > max_items:
            trimmed[key] = value[:max_items]
            trimmed[f"{key}_total"] = len(value)
            changed = True
    if not changed:
        return result
    return {**result, "summary": trimmed, "items_limited": True}


def _bounded_preview(item: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    # URL steps carry pages instead of summary. Both must respect the inline
    # budget; the complete collected item remains in full_results for readback.
    preview = {key: item[key] for key in ("ok", "logical_source", "actual_source") if key in item}
    preview.update(truncated=True, summary="")
    if len(json.dumps(preview, ensure_ascii=False)) > max_chars:
        return None
    body = item.get("summary", item.get("pages", item))
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    low, high = 0, min(len(text), max_chars)
    while low < high:
        middle = (low + high + 1) // 2
        preview["summary"] = text[:middle]
        if len(json.dumps(preview, ensure_ascii=False)) <= max_chars:
            low = middle
        else:
            high = middle - 1
    preview["summary"] = text[:low]
    return preview


__all__ = [
    "_actual_source",
    "_base_actual_sources",
    "_compact_results",
    "_failed",
    "_reflect_results",
    "_successful_actual_sources",
    "_summary_for",
]
