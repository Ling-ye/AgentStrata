"""Unified tool catalog: aggregate tool packs, MCP servers, and subagent presets
into a single browsable catalog for the console UI.

Read-only — no runtime Agent dependency.
"""
from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from console.control.discovery import repo_root
from console.control.yaml_io import load_yaml_or_empty

_SRC = str(repo_root() / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ToolBrief:
    name: str
    summary: str
    category: str = ""
    weight: str = "light"
    requires_role: str | None = None


@dataclass
class CatalogItem:
    id: str
    kind: str  # "tool_pack" | "tool_feature" | "mcp" | "subagent" | "workflow" | "prompt" | "context_source"
    surface: str  # "tools" | "prompts" | "agents" | "context"
    name: str
    description: str
    category: str
    tags: list[str] = field(default_factory=list)
    risk: str = ""
    has_tools: bool = False
    has_prompts: bool = False
    requires_env: list[str] = field(default_factory=list)
    infra_service_id: str = ""
    tools: list[ToolBrief] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tools"] = [asdict(t) for t in self.tools]
        return d


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

_CATEGORY_MAP: dict[str, str] = {
    "feishu": "飞书",
    "chat": "会话",
    "codebase": "代码",
    "unity_codebase": "代码",
    "windows_fs": "文件系统",
    "career": "职业",
}

_MCP_CATEGORY_MAP: dict[str, str] = {
    "xiaohongshu-search": "搜索",
    "github-readonly": "代码",
    "playwright-browser": "浏览器",
}

_SUBAGENT_CATEGORY_MAP: dict[str, str] = {
    "code_explorer": "代码",
    "task_planner": "代码",
    "code_implementer": "代码",
    "code_reviewer": "代码",
    "test_runner": "代码",
    "code_publisher": "代码",
    "mcp_query": "MCP",
    "browser_reader": "浏览器",
}


def _cap_category(cap_id: str) -> str:
    ns = cap_id.split(".")[0] if "." in cap_id else cap_id
    return _CATEGORY_MAP.get(ns, ns)


# ---------------------------------------------------------------------------
# Tool pack collection
# ---------------------------------------------------------------------------

def _collect_tool_packs() -> list[CatalogItem]:
    from chatcopilot.component_catalog import iter_tool_pack_tools, iter_tool_packs

    items: list[CatalogItem] = []
    for pack_id, entry in iter_tool_packs():
        tools = [
            ToolBrief(
                name=tool.name,
                summary=tool.summary,
                category=tool.category,
                weight=tool.weight,
                requires_role=tool.requires_role,
            )
            for tool in iter_tool_pack_tools(pack_id)
        ]

        items.append(CatalogItem(
            id=f"tool_pack:{pack_id}",
            kind="tool_pack",
            surface="tools",
            name=pack_id,
            description=entry.description,
            category=_cap_category(pack_id),
            has_tools=bool(entry.tool_names),
            has_prompts=bool(entry.manifest_module),
            tools=tools,
        ))
    return items


def _collect_tool_features() -> list[CatalogItem]:
    from chatcopilot.component_catalog import iter_tool_features

    return [
        CatalogItem(
            id=f"tool_feature:{feature_id}",
            kind="tool_feature",
            surface="tools",
            name=feature_id,
            description=entry.description,
            category=_cap_category(feature_id),
        )
        for feature_id, entry in iter_tool_features()
    ]


# ---------------------------------------------------------------------------
# MCP catalog collection
# ---------------------------------------------------------------------------

def _collect_mcp_servers() -> list[CatalogItem]:
    from chatcopilot.component_catalog import iter_mcp_catalog_entries

    items: list[CatalogItem] = []
    for catalog_id, entry in iter_mcp_catalog_entries():
        server = entry.server
        search_only = list(server.get("search_only_tools") or [])
        tools = [ToolBrief(name=str(t), summary="MCP remote tool") for t in search_only]

        items.append(CatalogItem(
            id=f"mcp:{catalog_id}",
            kind="mcp",
            surface="tools",
            name=entry.title or catalog_id,
            description=f"Transport: {server.get('transport', 'stdio')}, URL: {server.get('url', 'N/A')}",
            category=_MCP_CATEGORY_MAP.get(catalog_id, "外部服务"),
            tags=[str(server.get("risk", ""))],
            risk=str(server.get("risk") or entry.risk or ""),
            has_tools=bool(search_only),
            infra_service_id=str(server.get("id", "")),
            requires_env=list(entry.env_examples.keys()),
            tools=tools,
        ))
    return items


# ---------------------------------------------------------------------------
# Subagent preset collection
# ---------------------------------------------------------------------------

def _collect_subagents() -> list[CatalogItem]:
    from chatcopilot.component_catalog import iter_subagent_presets

    items: list[CatalogItem] = []
    for name, preset in iter_subagent_presets():
        items.append(CatalogItem(
            id=f"sub:{name}",
            kind="subagent",
            surface="agents",
            name=name,
            description=preset.summary or "",
            category=_SUBAGENT_CATEGORY_MAP.get(name, "子代理"),
            tags=list(preset.workflow_tags or ()),
            has_tools=True,
            tools=[ToolBrief(name=preset.tool_name, summary=preset.summary or "")],
        ))
    return items


def _collect_workflows() -> list[CatalogItem]:
    from chatcopilot.component_catalog import iter_workflows

    return [
        CatalogItem(
            id=f"workflow:{name}",
            kind="workflow",
            surface="agents",
            name=name,
            description=workflow.summary or "",
            category="Workflow",
            tags=list(workflow.steps),
            has_tools=True,
            tools=[ToolBrief(name=workflow.tool_name, summary=workflow.summary or "")],
        )
        for name, workflow in iter_workflows()
    ]


# ---------------------------------------------------------------------------
# BotSpec surface collection
# ---------------------------------------------------------------------------

def _collect_prompt_items() -> list[CatalogItem]:
    return [
        CatalogItem(
            id="prompts:persona",
            kind="prompt",
            surface="prompts",
            name="Persona",
            description="机器人身份、边界、领域范围和主要交互风格提示词。",
            category="提示词",
            has_prompts=True,
        ),
        CatalogItem(
            id="prompts:refusal",
            kind="prompt",
            surface="prompts",
            name="Refusal",
            description="拒答策略提示词；未配置时使用框架默认策略。",
            category="提示词",
            has_prompts=True,
        ),
        CatalogItem(
            id="prompts:safety",
            kind="prompt",
            surface="prompts",
            name="Safety",
            description="安全提示词覆盖；用于替换框架内置安全默认文本。",
            category="提示词",
            has_prompts=True,
        ),
        CatalogItem(
            id="prompts:roles",
            kind="prompt",
            surface="prompts",
            name="Roles",
            description="owner/admin/user 等角色行为提示词覆盖。",
            category="角色",
            has_prompts=True,
        ),
    ]


def _collect_context_items() -> list[CatalogItem]:
    return [
        CatalogItem(
            id="context:memory_store",
            kind="context_source",
            surface="context",
            name="Memory Store",
            description="BotSpec context.memory_store 声明的长期记忆 provider、namespace 和 schema。",
            category="记忆",
        ),
        CatalogItem(
            id="context:rag",
            kind="context_source",
            surface="context",
            name="RAG",
            description="本地或私有知识源清单，只注入当前 bot 声明的数据。",
            category="知识库",
        ),
        CatalogItem(
            id="context:wiki",
            kind="context_source",
            surface="context",
            name="Wiki",
            description="可写的 owner 私有 Markdown Wiki；原始来源留档，派生索引可重建。",
            category="知识库",
        ),
        CatalogItem(
            id="context:codebases",
            kind="context_source",
            surface="context",
            name="Codebases",
            description="bot-owned repository registry，仅供只读 codebase.read 查询。",
            category="代码",
        ),
        CatalogItem(
            id="context:playbooks",
            kind="context_source",
            surface="context",
            name="Playbooks",
            description="机器人 playbooks manifest，按需加载可复用技能正文。",
            category="技能",
        ),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_cache: list[CatalogItem] | None = None


def full_catalog(*, use_cache: bool = True) -> list[CatalogItem]:
    """Return the complete unified catalog."""
    global _cache
    if use_cache and _cache is not None:
        return _cache

    items = (
        _collect_tool_packs()
        + _collect_tool_features()
        + _collect_mcp_servers()
        + _collect_prompt_items()
        + _collect_subagents()
        + _collect_workflows()
        + _collect_context_items()
    )
    _cache = items
    return items


def catalog_item(item_id: str) -> CatalogItem | None:
    for item in full_catalog():
        if item.id == item_id:
            return item
    return None


def bot_tool_config(bot_yaml_path: Path) -> dict[str, Any]:
    """Read current tool config from a bot.yaml (four-surface BotSpec)."""
    data = load_yaml_or_empty(bot_yaml_path)

    tools_section = data.get("tools", {}) if isinstance(data.get("tools"), dict) else {}
    packs = tools_section.get("packs", []) if isinstance(tools_section, dict) else []
    features = tools_section.get("features", []) if isinstance(tools_section, dict) else []
    hide = tools_section.get("hide", []) if isinstance(tools_section, dict) else []

    mcp = tools_section.get("mcp", {}) if isinstance(tools_section.get("mcp"), dict) else {}
    servers_rel = mcp.get("servers") if isinstance(mcp, dict) else None
    mcp_servers: list[dict[str, Any]] = []
    if servers_rel:
        servers_path = bot_yaml_path.parent / str(servers_rel)
        servers_data = load_yaml_or_empty(servers_path)
        for item in servers_data.get("servers", []):
            if isinstance(item, dict) and item.get("ref"):
                mcp_servers.append({
                    "ref": str(item["ref"]),
                    "enabled": item.get("enabled", True) is not False,
                })

    agents = data.get("agents", {}) if isinstance(data.get("agents"), dict) else {}
    sa_include = agents.get("presets", agents.get("include", [])) if isinstance(agents, dict) else []
    workflows = agents.get("workflows", []) if isinstance(agents, dict) else []

    packs_out = [str(c).strip() for c in (packs if isinstance(packs, list) else [])]
    hide_out = [str(t).strip() for t in (hide if isinstance(hide, list) else [])]
    agent_presets_out = [str(s).strip() for s in (sa_include if isinstance(sa_include, list) else [])]
    workflows_out = [str(w).strip() for w in (workflows if isinstance(workflows, list) else [])]
    features_out = [str(f).strip() for f in (features if isinstance(features, list) else [])]

    return {
        "tools": {
            "packs": packs_out,
            "features": features_out,
            "hide": hide_out,
            "mcp": {"servers": mcp_servers},
        },
        "agents": {
            "presets": agent_presets_out,
            "workflows": workflows_out,
        },
    }
