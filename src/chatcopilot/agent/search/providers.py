"""Deterministic search-provider execution."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from chatcopilot.agent.search.relevance import filter_relevant_items
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.external_tools.shared.tool_spec import ToolDef

_LOG = logging.getLogger(__name__)

WEB_PROVIDER_PRIORITY = ("tavily", "brave", "searxng")
DIRECT_SEARCH_SERVERS: dict[str, tuple[str, ...]] = {
    "web": WEB_PROVIDER_PRIORITY,
    "experience": ("xiaohongshu",),
    "commerce": ("taoke",),
}
CIRCUIT_BREAKER_ERRORS = frozenset({
    "mcp_quota_exceeded",
    "mcp_unavailable",
    "mcp_timeout",
    "mcp_busy",
    "xhs_login_required",
})
MAX_RESULT_ITEMS = 15
_SEARCH_TEXT_FIELDS = ("query", "keyword", "q", "search", "term")


@dataclass(frozen=True)
class SearchProviderRegistry:
    tools: dict[str, ToolDef]
    _raw_search: dict[str, list[ToolDef]] = field(default_factory=dict)

    @classmethod
    def from_tools(
        cls,
        tools: Sequence[ToolDef],
        raw_mcp_tools: Sequence[ToolDef] = (),
    ) -> "SearchProviderRegistry":
        tool_dict = {tool.name: tool for tool in tools}
        raw: dict[str, list[ToolDef]] = {}
        for tool in raw_mcp_tools:
            server_id = str(tool.metadata.get("mcp_server_id", ""))
            remote = str(tool.metadata.get("mcp_remote_name", ""))
            search_only = list(tool.metadata.get("mcp_search_only_tools") or [])
            if server_id and remote and remote in search_only:
                raw.setdefault(server_id, []).append(tool)
        return cls(tool_dict, raw)

    def available_sources(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.has_direct_source("web") or any(
            name in self.tools for name in ("search_tavily", "search_brave", "search_searxng")
        ):
            out.append("web")
        for source, delegate in {
            "experience": "search_xiaohongshu",
            "commerce": "search_taoke",
            "github": "query_approved_sources",
        }.items():
            if delegate in self.tools or self.has_direct_source(source):
                out.append(source)
        if "web_fetch_page" in self.tools:
            out.append("url")
        return tuple(out)

    def has_direct_source(self, source: str) -> bool:
        return any(server_id in self._raw_search for server_id in DIRECT_SEARCH_SERVERS.get(source, ()))

    def raw_search_tool(self, server_id: str) -> ToolDef | None:
        candidates = self._raw_search.get(server_id, [])
        for tool in candidates:
            remote = str(tool.metadata.get("mcp_remote_name", ""))
            if "image" not in remote.lower():
                return tool
        return candidates[0] if candidates else None

    def delegate_for_source(self, source: str) -> ToolDef | None:
        if source == "web":
            for name in ("search_tavily", "search_brave", "search_searxng"):
                if name in self.tools:
                    return self.tools[name]
            return None
        name = {
            "experience": "search_xiaohongshu",
            "commerce": "search_taoke",
            "github": "query_approved_sources",
        }.get(source)
        return self.tools.get(name or "")

    def secondary_web_delegate(self, excluded_sources: set[str]) -> ToolDef | None:
        for name, source in (
            ("search_tavily", "tavily"),
            ("search_brave", "brave"),
            ("search_searxng", "searxng"),
        ):
            if source not in excluded_sources and name in self.tools:
                return self.tools[name]
        return None


class DirectSearchProvider:
    def __init__(
        self,
        *,
        registry: SearchProviderRegistry,
        circuit: SearchCircuitBreaker | None = None,
    ) -> None:
        self._registry = registry
        self._circuit = circuit

    def search(
        self,
        *,
        logical_source: str,
        query: str,
        exclude_servers: set[str] | None = None,
    ) -> dict[str, Any] | None:
        server_ids = DIRECT_SEARCH_SERVERS.get(logical_source)
        if not server_ids:
            return None
        has_any_tool = False
        attempts: list[dict[str, str]] = []
        for server_id in server_ids:
            if exclude_servers and server_id in exclude_servers:
                continue
            tool = self._registry.raw_search_tool(server_id)
            if tool is None:
                attempts.append({"server": server_id, "status": "not_configured"})
                continue
            has_any_tool = True
            block = self._circuit.blocked(server_id) if self._circuit is not None else None
            if block is not None:
                attempts.append({"server": server_id, "status": "circuit_open", "reason": block})
                continue
            payload, outputs, error_code = self._call_tool(tool, query)
            if error_code:
                if self._circuit is not None:
                    self._circuit.record_failure(server_id, error_code)
                attempts.append({"server": server_id, "status": "failed", "reason": error_code})
                continue
            if self._circuit is not None:
                self._circuit.record_success(server_id)
            items = _extract_search_items(payload)
            items = filter_relevant_items(
                items,
                query=query,
                min_score=1,
                max_items=MAX_RESULT_ITEMS,
            )
            return {
                "ok": True,
                "logical_source": logical_source,
                "actual_source": server_id,
                "summary": {
                    "query": query,
                    "items": items,
                    "item_count": len(items),
                    "provider_attempts": attempts,
                },
                "outputs": outputs,
            }
        if not has_any_tool:
            return None
        return {
            "ok": False,
            "logical_source": logical_source,
            "actual_source": logical_source,
            "error": "all_direct_search_sources_exhausted",
            "provider_attempts": attempts,
        }

    def _call_tool(
        self,
        tool: ToolDef,
        query: str,
    ) -> tuple[dict[str, Any], list[Any], str]:
        args = _build_raw_search_args(tool, query)
        try:
            summary, outputs, _hint = tool.handler(args)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("direct search handler raised | tool=%s error=%s", tool.name, exc)
            return {}, [], "mcp_unavailable"
        text = str(summary or "")
        content = _extract_mcp_body(text)
        payload = _parse_payload(content) or _parse_payload(text)
        if not payload:
            return {"results": []}, list(outputs), ""
        error_code = str(payload.get("error_code") or "")
        is_error = (
            payload.get("is_error")
            or payload.get("ok") is False
            or error_code in CIRCUIT_BREAKER_ERRORS
        )
        if is_error:
            return payload, list(outputs), error_code or "mcp_unavailable"
        return payload, list(outputs), ""


def _build_raw_search_args(tool: ToolDef, query: str) -> dict[str, Any]:
    props = tool.properties or {}
    args: dict[str, Any] = {
        _search_text_field(tool): _tighten_query(query, tool),
    }
    limit = 6 if str(tool.metadata.get("mcp_server_id", "")) == "searxng" else 10
    if "max_results" in props:
        args["max_results"] = limit
    if "limit" in props:
        args["limit"] = limit
    if "count" in props:
        args["count"] = limit
    if "search_depth" in props:
        args["search_depth"] = "basic"
    return args


def _search_text_field(tool: ToolDef) -> str:
    required = {str(item) for item in tool.required or ()}
    props = tool.properties or {}
    for name in _SEARCH_TEXT_FIELDS:
        if name in required:
            return name
    for name in _SEARCH_TEXT_FIELDS:
        if name in props:
            return name
    return "query"


def _tighten_query(query: str, tool: ToolDef) -> str:
    text = str(query or "").strip()
    server_id = str(tool.metadata.get("mcp_server_id", ""))
    if server_id != "searxng":
        return text
    noise_exclusions = ("pinterest.com", "facebook.com", "instagram.com")
    suffix = " ".join(f"-site:{host}" for host in noise_exclusions)
    return f"{text} {suffix}".strip()


def _parse_payload(summary: str) -> dict[str, Any]:
    try:
        value = json.loads(summary)
    except (TypeError, ValueError):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"results": value}
    return {}


def _extract_mcp_body(text: str) -> str:
    marker = "returned:\n"
    idx = text.find(marker)
    if idx >= 0:
        return text[idx + len(marker):]
    return text


def _extract_search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for candidate in _payload_candidates(payload):
        items = _extract_search_items_from(candidate)
        if items:
            return items
    return []


def _payload_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            out.append(value)
            for key in ("structured", "structuredContent", "result", "data"):
                add(value.get(key))
            content = value.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    add(item.get("structured"))
                    text = item.get("text")
                    if isinstance(text, str):
                        parsed = _parse_payload(text)
                        if parsed:
                            add(parsed)
        elif isinstance(value, list):
            out.append({"results": value})

    add(payload)
    return out


def _extract_search_items_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("results", "items", "data", "feeds", "notes", "list", "organic_results"):
        candidates = payload.get(key)
        if not isinstance(candidates, list):
            continue
        out: list[dict[str, Any]] = []
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            entry = _flatten_search_entry(entry)
            url = _entry_url(entry)
            if not url:
                continue
            out.append({
                "title": _entry_title(entry),
                "url": url,
                "snippet": _entry_snippet(entry),
            })
        if out:
            return out
    return []


def _flatten_search_entry(entry: dict[str, Any]) -> dict[str, Any]:
    card = entry.get("note_card") or entry.get("noteCard")
    if isinstance(card, dict):
        return {**card, **entry}
    return entry


def _entry_url(entry: dict[str, Any]) -> str:
    for key in (
        "url",
        "link",
        "href",
        "share_link",
        "shareLink",
        "note_url",
        "noteUrl",
        "web_url",
        "webUrl",
    ):
        url = str(entry.get(key) or "").strip()
        if url:
            return url
    note_id = str(entry.get("note_id") or entry.get("noteId") or entry.get("id") or "").strip()
    if note_id and _looks_like_xhs_entry(entry):
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    return ""


def _looks_like_xhs_entry(entry: dict[str, Any]) -> bool:
    return any(
        key in entry
        for key in (
            "xsec_token", "xsecToken",
            "note_card", "noteCard",
            "interact_info", "interactInfo",
            "display_title", "displayTitle",
        )
    )


def _entry_title(entry: dict[str, Any]) -> str:
    for key in ("title", "name", "display_title", "displayTitle", "desc", "description"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return ""


def _entry_snippet(entry: dict[str, Any]) -> str:
    for key in ("content", "snippet", "description", "desc", "display_title", "displayTitle", "title"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value[:500]
    return ""


__all__ = [
    "CIRCUIT_BREAKER_ERRORS",
    "DIRECT_SEARCH_SERVERS",
    "DirectSearchProvider",
    "MAX_RESULT_ITEMS",
    "SearchProviderRegistry",
    "WEB_PROVIDER_PRIORITY",
]
