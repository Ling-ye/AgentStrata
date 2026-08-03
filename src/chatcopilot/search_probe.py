"""Standalone MCP search probes.

This module intentionally bypasses AgentRuntime, search_information, router,
subagents, and reranking.  It only uses the MCP transport layer to verify the
raw search tools bound by a bot spec.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from chatcopilot.agent.mcp.client import McpToolProvider
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.mcp import load_mcp_server_configs
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.external_tools.shared.tool_spec import ToolDef

_DEFAULT_BOT = "bots/lingye-copilot-qq/bot.yaml"
_DEFAULT_QUERY = "上海 二郎拉面 探店"
_DEFAULT_URL = "https://example.com"
_TEXT_FIELDS = (
    "query",
    "keyword",
    "q",
    "search",
    "term",
    "word",
    "text",
    "name",
    "goods_name",
    "goodsName",
)
_URL_FIELDS = ("url", "urls", "link", "links")
_LIMIT_FIELDS = (
    "max_results",
    "limit",
    "count",
    "page_size",
    "pageSize",
    "size",
    "num",
)
_ITEM_KEYS = (
    "results",
    "items",
    "data",
    "feeds",
    "notes",
    "list",
    "goods",
    "goods_list",
    "map_data",
    "organic_results",
)
@dataclass(frozen=True)
class ProbeResult:
    server_id: str
    tool_name: str
    local_name: str
    status: str
    ok: bool
    arguments: dict[str, Any]
    result_count: int
    error_code: str = ""
    message: str = ""
    sample: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "local_name": self.local_name,
            "status": self.status,
            "ok": self.ok,
            "arguments": self.arguments,
            "result_count": self.result_count,
            "error_code": self.error_code,
            "message": self.message,
            "sample": self.sample,
        }


def main(argv: Sequence[str] | None = None) -> int:
    _prefer_utf8_stdio()
    args = _parse_args(argv)
    configs = _load_search_configs(
        Path(args.bot),
        include_servers=tuple(args.server or ()),
    )
    if not configs:
        print("No enabled search MCP servers found.", file=sys.stderr)
        return 2

    provider = McpToolProvider(configs)
    try:
        tools = provider.load_tools()
        results = run_probes(
            configs,
            tools,
            query=args.query,
            url=args.url,
            require_results=not args.allow_empty,
            strict_contextual=args.strict_contextual,
        )
    finally:
        provider.close()

    payload = {
        "ok": all(item.ok for item in results),
        "bot": args.bot,
        "query": args.query,
        "results": [item.to_dict() for item in results],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(payload)
    return 0 if payload["ok"] else 1


def _prefer_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def run_probes(
    configs: Sequence[McpServerConfig],
    tools: Sequence[ToolDef],
    *,
    query: str,
    url: str,
    require_results: bool,
    strict_contextual: bool = False,
) -> list[ProbeResult]:
    by_server_tool = {
        (
            str(tool.metadata.get("mcp_server_id") or ""),
            str(tool.metadata.get("mcp_remote_name") or ""),
        ): tool
        for tool in tools
    }
    context: dict[str, Any] = {}
    results: list[ProbeResult] = []
    for config in configs:
        for remote_name in _probe_order(config.search_only_tools):
            tool = by_server_tool.get((config.id, remote_name))
            if tool is None:
                results.append(
                    ProbeResult(
                        server_id=config.id,
                        tool_name=remote_name,
                        local_name=config.tool_prefix + remote_name,
                        status="missing",
                        ok=False,
                        arguments={},
                        result_count=0,
                        error_code="tool_missing",
                        message="Tool was declared in search_only_tools but not listed by MCP server.",
                    )
                )
                continue
            result = _probe_tool(
                config,
                tool,
                query=query,
                url=url,
                context=context,
                require_results=require_results,
                strict_contextual=strict_contextual,
            )
            results.append(result)
    return results


def _probe_order(tool_names: Sequence[str]) -> tuple[str, ...]:
    order = {
        "search_feeds": 0,
    }
    return tuple(sorted(tool_names, key=lambda name: (order.get(name, 1), name)))


def _probe_tool(
    config: McpServerConfig,
    tool: ToolDef,
    *,
    query: str,
    url: str,
    context: dict[str, Any],
    require_results: bool,
    strict_contextual: bool,
) -> ProbeResult:
    remote_name = str(tool.metadata.get("mcp_remote_name") or tool.name)
    try:
        arguments = _build_probe_args(tool, query=query, url=url, context=context)
    except ValueError as exc:
        is_contextual = _is_contextual_tool(remote_name)
        return ProbeResult(
            server_id=config.id,
            tool_name=remote_name,
            local_name=tool.name,
            status="skipped",
            ok=is_contextual and not strict_contextual,
            arguments={},
            result_count=0,
            error_code="context_unavailable" if is_contextual else "unsupported_schema",
            message=str(exc),
        )

    try:
        summary, _outputs, _hint = tool.handler(arguments)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            server_id=config.id,
            tool_name=remote_name,
            local_name=tool.name,
            status="failed",
            ok=False,
            arguments=arguments,
            result_count=0,
            error_code=type(exc).__name__,
            message=str(exc),
        )

    parsed = _parse_summary(summary)
    error_code = _error_code(parsed)
    items = _collect_items(parsed)
    result_count = len(items)
    sample = _sample_text(parsed, items)
    expects_items = _expects_items(tool)
    empty_is_failure = require_results and expects_items
    ok = not error_code and (result_count > 0 or not empty_is_failure)
    status = "passed" if ok else "empty" if not error_code else "failed"
    return ProbeResult(
        server_id=config.id,
        tool_name=remote_name,
        local_name=tool.name,
        status=status,
        ok=ok,
        arguments=arguments,
        result_count=result_count,
        error_code=error_code,
        message="" if ok else "Tool returned no extractable results." if not error_code else sample,
        sample=sample,
    )


def _build_probe_args(
    tool: ToolDef,
    *,
    query: str,
    url: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    remote_name = str(tool.metadata.get("mcp_remote_name") or tool.name)
    if remote_name == "check_login_status":
        return {}
    props = tool.properties or {}
    required = [item for item in tool.required if item in props]
    out: dict[str, Any] = {}

    for field in required:
        out[field] = _value_for_field(field, props.get(field) or {}, query=query, url=url)
    if not any(field in out for field in _TEXT_FIELDS):
        for field in _TEXT_FIELDS:
            if field in props:
                out[field] = query
                break
    for field in _LIMIT_FIELDS:
        if field in props and field not in out:
            out[field] = _limit_for_field(field)
    if "search_depth" in props and "search_depth" not in out:
        out["search_depth"] = "basic"
    return out


def _value_for_field(field: str, prop: dict[str, Any], *, query: str, url: str) -> Any:
    if field in _TEXT_FIELDS:
        return query
    if field in _URL_FIELDS:
        return [url] if prop.get("type") == "array" else url
    if field in _LIMIT_FIELDS:
        return _limit_for_field(field)
    if prop.get("type") == "integer":
        return 1
    if prop.get("type") == "number":
        return 1.0
    if prop.get("type") == "boolean":
        return False
    if prop.get("type") == "array":
        return [query]
    if prop.get("type") == "object":
        return {}
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    return query


def _limit_for_field(field: str) -> int:
    return 10 if field in {"page_size", "pageSize"} else 5


def _parse_summary(summary: Any) -> Any:
    text = str(summary or "")
    marker = "returned:\n"
    idx = text.find(marker)
    if idx >= 0:
        text = text[idx + len(marker):]
    return _parse_json(text) or {"text": text}


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _error_code(value: Any) -> str:
    for item in _walk(value):
        if not isinstance(item, dict):
            continue
        code = str(item.get("error_code") or "")
        if code:
            return code
        if item.get("is_error") or item.get("ok") is False:
            return "mcp_error"
    return ""


def _collect_items(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _walk(value):
        if isinstance(item, dict):
            for key in _ITEM_KEYS:
                entries = item.get(key)
                if isinstance(entries, list):
                    out.extend(entry for entry in entries if isinstance(entry, dict))
            text = item.get("text")
            if isinstance(text, str):
                parsed = _parse_json(text)
                if parsed is not None:
                    out.extend(_collect_items(parsed))
                else:
                    out.extend(_items_from_truncated_text(text))
        elif isinstance(item, list):
            out.extend(entry for entry in item if isinstance(entry, dict))
    return _dedupe_items(out)


def _items_from_truncated_text(text: str) -> list[dict[str, Any]]:
    markers = (
        '"goods_name"',
        '\\"goods_name\\"',
        '"title"',
        '\\"title\\"',
        '"url"',
        '\\"url\\"',
        '"displayTitle"',
        '\\"displayTitle\\"',
        '"display_title"',
        '\\"display_title\\"',
    )
    count = max(text.count(marker) for marker in markers)
    if count <= 0:
        return []
    return [{"text_match": idx + 1, "sample": text[:200]} for idx in range(count)]


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)[:500]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _sample_text(value: Any, items: list[dict[str, Any]]) -> str:
    if items:
        return json.dumps(items[0], ensure_ascii=False)[:500]
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text[:500]


def _expects_items(tool: ToolDef) -> bool:
    remote_name = str(tool.metadata.get("mcp_remote_name") or tool.name)
    if remote_name in {"check_login_status", "get_feed_detail"}:
        return False
    return True


def _is_contextual_tool(remote_name: str) -> bool:
    return False


def _load_search_configs(
    bot_path: Path,
    *,
    include_servers: tuple[str, ...],
) -> tuple[McpServerConfig, ...]:
    spec = load_botspec(bot_path)
    wanted = set(include_servers)
    return tuple(
        config
        for config in load_mcp_server_configs(spec)
        if config.risk == "search"
        and config.search_only_tools
        and (not wanted or config.id in wanted)
    )


def _print_human(payload: dict[str, Any]) -> None:
    print(f"bot: {payload['bot']}")
    print(f"query: {payload['query']}")
    print(f"ok: {str(payload['ok']).lower()}")
    for item in payload["results"]:
        status = "OK" if item["ok"] else "FAIL"
        print(
            f"- [{status}] {item['server_id']}/{item['tool_name']} "
            f"status={item['status']} results={item['result_count']}"
        )
        if item["error_code"]:
            print(f"  error_code: {item['error_code']}")
        if item["message"]:
            print(f"  message: {item['message']}")
        if item["sample"]:
            print(f"  sample: {item['sample']}")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe enabled search MCP tools without running the bot agent.",
    )
    parser.add_argument("--bot", default=_DEFAULT_BOT, help="BotSpec path.")
    parser.add_argument("--query", default=_DEFAULT_QUERY, help="Search query.")
    parser.add_argument("--url", default=_DEFAULT_URL, help="URL used for URL-reader tools.")
    parser.add_argument(
        "--server",
        action="append",
        help="Only probe this MCP server id. Can be passed multiple times.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Treat successful empty search responses as pass.",
    )
    parser.add_argument(
        "--strict-contextual",
        action="store_true",
        help="Fail when contextual tools such as get_feed_detail cannot be probed.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
