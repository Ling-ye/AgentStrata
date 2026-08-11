"""Deterministic search-provider execution."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Sequence

from chatcopilot.agent.search.relevance import filter_relevant_items
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.contracts.subagents import SearchProviderSpec
from chatcopilot.external_tools.shared.tool_spec import ToolDef

_LOG = logging.getLogger(__name__)

WEB_PROVIDER_PRIORITY = ("tavily", "brave", "searxng")
DEFAULT_PROVIDER_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "searxng": "http://127.0.0.1:18064",
}
DEFAULT_PROVIDER_CREDENTIAL_ENVS = {
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_API_KEY",
    "searxng": "",
}
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
    "search_authentication_failed",
    "search_quota_exceeded",
    "search_timeout",
    "search_transport_error",
    "search_invalid_response",
    "search_invalid_configuration",
})
MAX_RESULT_ITEMS = 15
_MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024
_SEARCH_TEXT_FIELDS = ("query", "keyword", "q", "search", "term")
_PROVIDER_BREAKER_ERRORS = {
    "search_quota_exceeded": "mcp_quota_exceeded",
    "search_timeout": "mcp_timeout",
    "search_transport_error": "mcp_unavailable",
    "search_invalid_response": "mcp_unavailable",
    "search_authentication_failed": "mcp_unavailable",
    "search_invalid_configuration": "mcp_unavailable",
}


class SearchProviderError(RuntimeError):
    """Stable, non-secret failure raised by an in-process provider client."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class HttpSearchProviderClient:
    """Bounded stdlib HTTP client for one reviewed web-search API."""

    __slots__ = ("_credential", "spec")

    def __init__(self, spec: SearchProviderSpec, credential: str = "") -> None:
        self.spec = spec
        self._credential = credential

    def search(self, query: str) -> dict[str, Any]:
        request, disable_proxy = self._build_request(query)
        try:
            payload = _request_json(
                request,
                timeout_seconds=self.spec.timeout_seconds,
                disable_proxy=disable_proxy,
            )
            return {"results": _normalize_provider_results(self.spec, payload)}
        except SearchProviderError:
            raise
        except urllib.error.HTTPError as exc:
            raise SearchProviderError(_http_error_code(exc.code)) from None
        except (TimeoutError, socket.timeout):
            raise SearchProviderError("search_timeout") from None
        except (urllib.error.URLError, OSError):
            raise SearchProviderError("search_transport_error") from None
        except (UnicodeError, ValueError, TypeError):
            raise SearchProviderError("search_invalid_response") from None

    def _build_request(self, query: str) -> tuple[urllib.request.Request, bool]:
        endpoint = _validated_provider_endpoint(self.spec)
        bounded_query = str(query or "").strip()[:1000]
        headers = {"Accept": "application/json", "User-Agent": "AgentStrata/1.0"}
        if self.spec.kind == "tavily":
            headers["Authorization"] = f"Bearer {self._credential}"
            headers["Content-Type"] = "application/json"
            body = json.dumps(
                {
                    "query": bounded_query,
                    "search_depth": "basic",
                    "max_results": self.spec.max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                }
            ).encode("utf-8")
            return urllib.request.Request(endpoint, data=body, headers=headers), False
        if self.spec.kind == "brave":
            headers["X-Subscription-Token"] = self._credential
            url = _append_query(
                endpoint,
                {"q": bounded_query[:400], "count": self.spec.max_results},
            )
            return urllib.request.Request(url, headers=headers), False
        if self._credential:
            headers["Authorization"] = f"Bearer {self._credential}"
        search_endpoint = (
            endpoint
            if endpoint.rstrip("/").endswith("/search")
            else f"{endpoint.rstrip('/')}/search"
        )
        url = _append_query(
            search_endpoint,
            {
                "q": bounded_query,
                "format": "json",
                "categories": "general",
                "language": "auto",
                "safesearch": 1,
            },
        )
        return urllib.request.Request(url, headers=headers), True


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so credential headers never cross an origin boundary."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _request_json(
    request: urllib.request.Request,
    *,
    timeout_seconds: float,
    disable_proxy: bool,
) -> dict[str, Any]:
    handlers: list[Any] = [_NoRedirectHandler()]
    if disable_proxy:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(request, timeout=timeout_seconds) as response:
        raw = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_PROVIDER_RESPONSE_BYTES:
        raise SearchProviderError("search_invalid_response")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise SearchProviderError("search_invalid_response")
    return payload


def _append_query(endpoint: str, values: dict[str, Any]) -> str:
    return f"{endpoint}?{urllib.parse.urlencode(values)}"


def _validated_provider_endpoint(spec: SearchProviderSpec) -> str:
    if not (1.0 <= spec.timeout_seconds <= 60.0) or not (
        1 <= spec.max_results <= MAX_RESULT_ITEMS
    ):
        raise SearchProviderError("search_invalid_configuration")
    endpoint = spec.endpoint or DEFAULT_PROVIDER_ENDPOINTS.get(spec.kind, "")
    if spec.kind in {"tavily", "brave"}:
        if endpoint != DEFAULT_PROVIDER_ENDPOINTS[spec.kind]:
            raise SearchProviderError("search_invalid_configuration")
        return endpoint
    if spec.kind != "searxng":
        raise SearchProviderError("search_invalid_configuration")
    try:
        parsed = urllib.parse.urlparse(endpoint)
        _ = parsed.port
    except ValueError:
        raise SearchProviderError("search_invalid_configuration") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not _is_loopback_host(parsed.hostname)
    ):
        raise SearchProviderError("search_invalid_configuration")
    return endpoint


def _is_loopback_host(hostname: str) -> bool:
    if hostname.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _http_error_code(status: int) -> str:
    if status in {401, 403}:
        return "search_authentication_failed"
    if status in {402, 429, 432, 433}:
        return "search_quota_exceeded"
    if status in {408, 504}:
        return "search_timeout"
    return "search_transport_error"


def _normalize_provider_results(
    spec: SearchProviderSpec,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    container = payload
    if spec.kind == "brave":
        web = payload.get("web")
        if not isinstance(web, dict):
            raise SearchProviderError("search_invalid_response")
        container = web
    raw_results = container.get("results")
    if not isinstance(raw_results, list):
        raise SearchProviderError("search_invalid_response")
    results: list[dict[str, Any]] = []
    for raw in raw_results[: spec.max_results]:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or raw.get("link") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        title = str(raw.get("title") or raw.get("name") or "").strip()[:500]
        snippet = str(
            raw.get("content")
            or raw.get("description")
            or raw.get("snippet")
            or ""
        ).strip()[:1000]
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


@dataclass(frozen=True)
class SearchProviderRegistry:
    tools: dict[str, ToolDef]
    _raw_search: dict[str, list[ToolDef]] = field(default_factory=dict)
    _in_process: dict[str, HttpSearchProviderClient] = field(default_factory=dict)
    _provider_kinds: dict[str, str] = field(default_factory=dict)
    _provider_order: tuple[str, ...] = ()
    _unavailable: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_tools(
        cls,
        tools: Sequence[ToolDef],
        raw_mcp_tools: Sequence[ToolDef] = (),
        provider_specs: Sequence[SearchProviderSpec] = (),
    ) -> "SearchProviderRegistry":
        tool_dict = {tool.name: tool for tool in tools}
        raw: dict[str, list[ToolDef]] = {}
        for tool in raw_mcp_tools:
            server_id = str(tool.metadata.get("mcp_server_id", ""))
            remote = str(tool.metadata.get("mcp_remote_name", ""))
            search_only = list(tool.metadata.get("mcp_search_only_tools") or [])
            if server_id and remote and remote in search_only:
                raw.setdefault(server_id, []).append(tool)
        clients: dict[str, HttpSearchProviderClient] = {}
        kinds: dict[str, str] = {}
        unavailable: dict[str, str] = {}
        order: list[str] = []
        for spec in provider_specs:
            if not spec.enabled:
                continue
            order.append(spec.id)
            kinds[spec.id] = spec.kind
            if spec.kind not in DEFAULT_PROVIDER_ENDPOINTS:
                unavailable[spec.id] = "search_invalid_configuration"
                continue
            credential_env = spec.credential_env
            if credential_env is None:
                credential_env = DEFAULT_PROVIDER_CREDENTIAL_ENVS.get(spec.kind, "")
            credential = os.environ.get(credential_env, "").strip() if credential_env else ""
            if credential_env and not credential:
                unavailable[spec.id] = "search_credential_missing"
                continue
            clients[spec.id] = HttpSearchProviderClient(spec, credential)
        return cls(
            tool_dict,
            raw,
            clients,
            kinds,
            tuple(order),
            unavailable,
        )

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
        if source == "web" and self._in_process:
            return True
        return any(
            server_id in self._raw_search
            for server_id in DIRECT_SEARCH_SERVERS.get(source, ())
        )

    def direct_server_ids(self, source: str) -> tuple[str, ...]:
        if source != "web":
            return DIRECT_SEARCH_SERVERS.get(source, ())
        configured_kinds = {
            self._provider_kinds[provider_id]
            for provider_id in self._in_process
            if provider_id in self._provider_kinds
        }
        legacy = (
            server_id
            for server_id in WEB_PROVIDER_PRIORITY
            if server_id in self._raw_search and server_id not in configured_kinds
        )
        return tuple((*self._provider_order, *legacy))

    def in_process_client(self, provider_id: str) -> HttpSearchProviderClient | None:
        return self._in_process.get(provider_id)

    def unavailable_reason(self, provider_id: str) -> str:
        return self._unavailable.get(provider_id, "")

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
        server_ids = self._registry.direct_server_ids(logical_source)
        if not server_ids:
            return None
        has_any_tool = False
        attempts: list[dict[str, str]] = []
        for server_id in server_ids:
            if exclude_servers and server_id in exclude_servers:
                continue
            client = self._registry.in_process_client(server_id)
            tool = self._registry.raw_search_tool(server_id) if client is None else None
            unavailable = self._registry.unavailable_reason(server_id)
            if client is None and tool is None:
                attempts.append(
                    {
                        "server": server_id,
                        "status": "unavailable" if unavailable else "not_configured",
                        **({"reason": unavailable} if unavailable else {}),
                    }
                )
                continue
            has_any_tool = True
            block = self._circuit.blocked(server_id) if self._circuit is not None else None
            if block is not None:
                attempts.append({"server": server_id, "status": "circuit_open", "reason": block})
                continue
            if client is not None:
                payload, outputs, error_code = self._call_in_process(client, query)
            else:
                payload, outputs, error_code = self._call_tool(tool, query)
            if error_code:
                if self._circuit is not None:
                    self._circuit.record_failure(
                        server_id,
                        _PROVIDER_BREAKER_ERRORS.get(error_code, error_code),
                    )
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

    @staticmethod
    def _call_in_process(
        client: HttpSearchProviderClient,
        query: str,
    ) -> tuple[dict[str, Any], list[Any], str]:
        try:
            return client.search(query), [], ""
        except SearchProviderError as exc:
            _LOG.debug(
                "in-process search provider failed | provider=%s error_code=%s",
                client.spec.id,
                exc.error_code,
            )
            return {}, [], exc.error_code

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
    "DEFAULT_PROVIDER_CREDENTIAL_ENVS",
    "DEFAULT_PROVIDER_ENDPOINTS",
    "DIRECT_SEARCH_SERVERS",
    "DirectSearchProvider",
    "HttpSearchProviderClient",
    "MAX_RESULT_ITEMS",
    "SearchProviderError",
    "SearchProviderRegistry",
    "WEB_PROVIDER_PRIORITY",
]
