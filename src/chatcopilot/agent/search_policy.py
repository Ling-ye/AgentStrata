"""Built-in, platform-neutral source routing guidance for search tasks."""

from __future__ import annotations

from collections.abc import Iterable

SEARCH_DOMAINS: tuple[str, ...] = ("general", "technical", "game", "consumer", "news")
_SEARCH_DOMAIN_SET = frozenset(SEARCH_DOMAINS)

SEARCH_DEPTH_LEVELS: tuple[str, ...] = ("quick", "standard", "thorough")

SEARCH_TASK_REQUIRED_FIELDS: tuple[str, ...] = (
    "domain",
    "target_sites",
    "time_window",
    "required_fields",
    "cross_check",
)

SEARCH_TASK_PROPERTIES: dict[str, dict] = {
    "domain": {
        "type": "string",
        "enum": list(SEARCH_DOMAINS),
        "description": "Search domain used to choose and rank sources.",
    },
    "depth": {
        "type": "string",
        "enum": list(SEARCH_DEPTH_LEVELS),
        "description": (
            "Search depth. 'quick': 1 search, no deep read. "
            "'standard': up to 2 searches + 1 deep read (default). "
            "'thorough': up to 3 searches + 2 deep reads."
        ),
        "default": "standard",
    },
    "target_sites": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Requested hostnames or named sites. Use an empty array when no site was requested."
        ),
    },
    "time_window": {
        "type": "string",
        "description": (
            "Concrete freshness window such as 'latest as of 2026-06-25', "
            "'past 30 days', or 'not time-sensitive'."
        ),
    },
    "required_fields": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Facts useful results must provide, such as title, URL, date, version, or price."
        ),
    },
    "cross_check": {
        "type": "boolean",
        "description": (
            "Whether the main agent requires an independent second source. Use true for "
            "latest, comparison, recommendation, or consequential factual searches."
        ),
    },
}


def validate_write_task_args(args: dict) -> tuple[str, ...]:
    """Return validation errors for a delegate whose selector includes write tools.

    Subagents that can mutate state (e.g. ``code_implementer``) require an
    explicit ``write_scope`` so the implementer knows its mutation boundary.
    """
    errors: list[str] = []
    if not str(args.get("objective") or args.get("task") or "").strip():
        errors.append("objective cannot be empty")
    write_scope = args.get("write_scope")
    if write_scope is None or not str(write_scope).strip():
        errors.append("write_scope is required for write-capable subagents")
    return tuple(errors)


def validate_search_task_args(args: dict) -> tuple[str, ...]:
    """Return validation errors for one search delegate task pack."""

    errors: list[str] = []
    if not str(args.get("objective") or args.get("task") or "").strip():
        errors.append("objective cannot be empty")
    missing = [name for name in SEARCH_TASK_REQUIRED_FIELDS if name not in args]
    if missing:
        errors.append("missing required fields: " + ", ".join(missing))

    domain_value = args.get("domain")
    domain = str(domain_value or "").strip()
    if "domain" in args:
        if not isinstance(domain_value, str):
            errors.append("domain must be a string")
        elif domain not in _SEARCH_DOMAIN_SET:
            errors.append("domain must be one of: " + ", ".join(SEARCH_DOMAINS))

    target_sites = args.get("target_sites")
    if "target_sites" in args:
        if not isinstance(target_sites, (list, tuple)):
            errors.append("target_sites must be an array")
        elif any(not isinstance(value, str) or not value.strip() for value in target_sites):
            errors.append("target_sites must contain non-empty strings")

    time_window = args.get("time_window")
    if "time_window" in args:
        if not isinstance(time_window, str):
            errors.append("time_window must be a string")
        elif not time_window.strip():
            errors.append("time_window cannot be empty")

    required_fields = args.get("required_fields")
    if "required_fields" in args:
        if not isinstance(required_fields, (list, tuple)):
            errors.append("required_fields must be an array")
        elif not required_fields:
            errors.append("required_fields cannot be empty")
        elif any(
            not isinstance(value, str) or not value.strip() for value in required_fields
        ):
            errors.append("required_fields must contain non-empty strings")

    if "cross_check" in args and not isinstance(args.get("cross_check"), bool):
        errors.append("cross_check must be a boolean")
    return tuple(errors)


def render_search_routing_policy(tool_names: Iterable[str]) -> str:
    """Render routing guidance using only tools available in this session."""

    available = frozenset(str(name).strip() for name in tool_names if str(name).strip())
    if "search_information" in available:
        return "\n".join(
            [
                "## 统一搜索入口",
                "",
                "事实查证、最新信息、网页读取、GitHub 查询、商品与生活体验检索，"
                "统一调用 `search_information`。",
                "传入具体 `objective`；已有 URL 写入 `urls`；用户明确指定来源时使用"
                "逻辑 `source_hints`：`web`、`experience`、`commerce`、`github`、`url`。",
                "用户明确要求小红书 / XHS / Xiaohongshu 时，必须设置"
                "`source_hints=[\"experience\"]`；不要声称内部 MCP 暴露策略不可用。",
                "不要猜测或提及内部搜索供应商。搜索协调器会选择可用来源、静态读取、"
                "动态网页读取、供应商降级和必要的交叉核实。",
            ]
        )
    search_tools = sorted(name for name in available if name.startswith("search_"))
    if not search_tools:
        return ""

    lines = [
        "## 领域搜索路由",
        "",
        "调用任何 `search_*` 工具时，必须在 task pack 中传递：",
        "`domain`、`target_sites`、`time_window`、`required_fields`、`cross_check`。",
        "",
        "本会话可用的搜索入口：" + "、".join(f"`{name}`" for name in search_tools) + "。",
    ]

    web_sources = [
        name
        for name in ("search_tavily", "search_brave", "search_searxng")
        if name in available
    ]
    if "query_approved_sources" in available or web_sources:
        technical: list[str] = ["- `technical`：官方文档和项目一手资料优先。"]
        if "query_approved_sources" in available:
            technical.append(
                "GitHub 仓库、Issue、PR、Release 使用 `query_approved_sources`。"
            )
        if web_sources:
            technical.append("普通技术网页使用 " + "，不可用时再用 ".join(
                f"`{name}`" for name in web_sources
            ) + "。")
        lines.append("".join(technical))
    if web_sources:
        lines.append(
            "- `game`：官方网站、官方公告、可信 Wiki、社区讨论依次降级；使用 "
            + "，不可用时再用 ".join(f"`{name}`" for name in web_sources)
            + "。"
        )

    consumer_sources: list[str] = []
    if "search_taoke" in available:
        consumer_sources.append("商品价格、规格和链接使用 `search_taoke`")
    if "search_xiaohongshu" in available:
        consumer_sources.append("真实体验和生活口碑使用 `search_xiaohongshu`")
    if "search_tavily" in available:
        consumer_sources.append("官方参数和专业评测使用 `search_tavily`")
    if consumer_sources:
        lines.append("- `consumer`：" + "；".join(consumer_sources) + "。")
    if web_sources:
        lines.append(
            "- `general`：使用 "
            + "，不可用时再用 ".join(f"`{name}`" for name in web_sources)
            + "。"
        )

    lines.extend(
        [
            "",
            "用户指定网站时写入 `target_sites`，不要改用不匹配的来源。只有“最新”、比较、",
            "推荐或用户明确要求核实时才把 `cross_check` 设为 true，并由主 Agent 调用第二个",
            "已存在的独立来源；单个搜索 subagent 不负责跨来源编排。",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "SEARCH_DEPTH_LEVELS",
    "SEARCH_TASK_PROPERTIES",
    "SEARCH_TASK_REQUIRED_FIELDS",
    "render_search_routing_policy",
    "validate_search_task_args",
    "validate_write_task_args",
]
