"""Agent 工具注册中心：合并 builtin + external_tools（领域）+ MCP client。

每个工具源在自己的模块里通过模块级 ``TOOLS`` list 导出 ``ToolDef``，本中心负责
按 BotSpec 的 tool pack 名解析模块路径并 import 合并。MCP client 在占位阶段
返回空列表，预留扩展位。
"""
from __future__ import annotations

import importlib
import logging
from typing import Dict, List, Optional, Sequence, Tuple

from chatcopilot.agent.tools.builtin import resolve_builtin_tool_modules
from chatcopilot.tool_packs.catalog import all_tool_modules, resolve_tool_modules
from chatcopilot.external_tools.shared.tool_spec import (
    ToolDef,
    build_mcp_schema,
    build_openai_schema,
)

_LOGGER = logging.getLogger("chatcopilot.agent.tools.registry")


def _import_module_tools(module_path: str) -> List[ToolDef]:
    try:
        mod = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("无法加载工具模块 %s: %s", module_path, exc)
        return []
    tools = getattr(mod, "TOOLS", None)
    if not tools:
        _LOGGER.warning("工具模块 %s 未导出 TOOLS", module_path)
        return []
    return [tool for tool in tools if isinstance(tool, ToolDef)]


def discover_tools(
    *,
    tool_packs: Sequence[str] | None = None,
    exclude_tools: Sequence[str] | None = None,
    mcp_tools: Sequence[ToolDef] | None = None,
) -> List[ToolDef]:
    """合并所有来源的工具列表。

    Args:
        tool_packs: 选定 BotSpec tool pack 时仅启用其映射的领域工具模块；
            为 None 时启用全部 tool pack 工具（与 builtin 合并）。
        exclude_tools: BotSpec 黑名单。
    """
    out: List[ToolDef] = []
    seen: set[str] = set()
    excluded = set(exclude_tools or ())

    # 1) builtin tools: selected by tool pack; None keeps MCP-compatible load-all.
    for module_path in resolve_builtin_tool_modules(tool_packs):
        for tool in _import_module_tools(module_path):
            if tool.name in excluded or tool.name in seen:
                continue
            seen.add(tool.name)
            out.append(tool)

    # 2) external_tools（领域）：按 tool pack 解析
    module_paths = (
        resolve_tool_modules(tuple(tool_packs))
        if tool_packs is not None
        else all_tool_modules()
    )
    for module_path in module_paths:
        for tool in _import_module_tools(module_path):
            if tool.name in excluded or tool.name in seen:
                continue
            seen.add(tool.name)
            out.append(tool)

    # 3) MCP client tools are injected by AgentRuntime after BotSpec assembly.
    for tool in mcp_tools or ():
        if tool.name in excluded or tool.name in seen:
            continue
        seen.add(tool.name)
        out.append(tool)

    return out


def build_tools_schema(
    *,
    tool_packs: Sequence[str] | None = None,
    exclude_tools: Sequence[str] | None = None,
    mcp_tools: Sequence[ToolDef] | None = None,
) -> Tuple[List[Dict[str, object]], Dict[str, ToolDef]]:
    tools = discover_tools(
        tool_packs=tool_packs,
        exclude_tools=exclude_tools,
        mcp_tools=mcp_tools,
    )
    schema = sorted(
        (build_openai_schema(tool) for tool in tools),
        key=lambda entry: str((entry.get("function") or {}).get("name") or ""),
    )
    index: Dict[str, ToolDef] = {tool.name: tool for tool in tools}
    return schema, index


def build_mcp_tools_schema(
    *,
    tool_packs: Sequence[str] | None = None,
    exclude_tools: Sequence[str] | None = None,
    mcp_tools: Sequence[ToolDef] | None = None,
) -> Tuple[List[Dict[str, object]], Dict[str, ToolDef]]:
    tools = discover_tools(
        tool_packs=tool_packs,
        exclude_tools=exclude_tools,
        mcp_tools=mcp_tools,
    )
    schema = sorted(
        (build_mcp_schema(tool) for tool in tools),
        key=lambda entry: str(entry.get("name") or ""),
    )
    index: Dict[str, ToolDef] = {tool.name: tool for tool in tools}
    return schema, index


def find_spec(
    name: str,
    *,
    tool_packs: Sequence[str] | None = None,
    exclude_tools: Sequence[str] | None = None,
    mcp_tools: Sequence[ToolDef] | None = None,
) -> Optional[ToolDef]:
    for tool in discover_tools(
        tool_packs=tool_packs,
        exclude_tools=exclude_tools,
        mcp_tools=mcp_tools,
    ):
        if tool.name == name:
            return tool
    return None


__all__ = [
    "build_mcp_tools_schema",
    "build_tools_schema",
    "discover_tools",
    "find_spec",
]
