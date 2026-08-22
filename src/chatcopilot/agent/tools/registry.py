"""Agent 工具注册中心：合并 builtin + external_tools（领域）+ MCP client。

每个工具源在自己的模块里通过模块级 ``TOOLS`` list 导出 ``ToolDef``，本中心负责
按 BotSpec 的 tool pack 名解析模块路径并 import 合并。MCP client 在占位阶段
返回空列表，预留扩展位。
"""

from __future__ import annotations

import importlib
from typing import Dict, List, Optional, Sequence, Tuple

from chatcopilot.tool_packs.catalog import (
    all_tool_bindings,
    get_tool_pack_entry,
    known_tool_pack_names,
    resolve_tool_bindings,
)
from chatcopilot.contracts.tools import (
    ToolDef,
    build_mcp_schema,
    build_openai_schema,
)


class ToolMaterializationError(RuntimeError):
    """An enabled catalog binding could not produce its exact ToolDef set."""

    def __init__(
        self,
        *,
        module: str,
        pack_names: Sequence[str],
        reason: str,
        tool_names: Sequence[str] = (),
    ) -> None:
        self.module = module
        self.pack_names = tuple(pack_names)
        self.reason = reason
        self.tool_names = tuple(tool_names)
        details = [f"module={module}", f"reason={reason}"]
        if self.pack_names:
            details.append("packs=" + ",".join(self.pack_names))
        if self.tool_names:
            details.append("tools=" + ",".join(self.tool_names))
        super().__init__("tool materialization failed: " + "; ".join(details))


def _import_module_tools(
    module_path: str,
    *,
    pack_names: Sequence[str],
) -> List[ToolDef]:
    try:
        mod = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001
        raise ToolMaterializationError(
            module=module_path,
            pack_names=pack_names,
            reason=f"import_error:{type(exc).__name__}",
        ) from exc
    tools = getattr(mod, "TOOLS", None)
    if not isinstance(tools, (list, tuple)) or not tools:
        raise ToolMaterializationError(
            module=module_path,
            pack_names=pack_names,
            reason="missing_or_empty_tools_export",
        )
    invalid = [type(tool).__name__ for tool in tools if not isinstance(tool, ToolDef)]
    if invalid:
        raise ToolMaterializationError(
            module=module_path,
            pack_names=pack_names,
            reason="invalid_tool_export",
            tool_names=invalid,
        )
    names = [tool.name for tool in tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ToolMaterializationError(
            module=module_path,
            pack_names=pack_names,
            reason="duplicate_tool_export",
            tool_names=duplicates,
        )
    return list(tools)


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

    # 1) Static tools: built-in and domain tools share one exact catalog projection.
    selected_packs = (
        tuple(tool_packs) if tool_packs is not None else tuple(sorted(known_tool_pack_names()))
    )
    unknown_packs = sorted(set(selected_packs) - known_tool_pack_names())
    if unknown_packs:
        raise ToolMaterializationError(
            module="chatcopilot.tool_packs.catalog",
            pack_names=unknown_packs,
            reason="unknown_tool_pack",
        )
    bindings = (
        resolve_tool_bindings(tuple(tool_packs)) if tool_packs is not None else all_tool_bindings()
    )
    for binding in bindings:
        allowed_names = frozenset(binding.tool_names)
        binding_packs = tuple(
            pack_name
            for pack_name in selected_packs
            if (
                (entry := get_tool_pack_entry(pack_name)) is not None
                and any(item.module == binding.module for item in entry.tool_bindings)
            )
        )
        module_tools = _import_module_tools(binding.module, pack_names=binding_packs)
        materialized_names = {tool.name for tool in module_tools}
        missing = sorted(allowed_names - materialized_names)
        if missing:
            raise ToolMaterializationError(
                module=binding.module,
                pack_names=binding_packs,
                reason="declared_tools_missing",
                tool_names=missing,
            )
        for tool in module_tools:
            if tool.name not in allowed_names:
                continue
            if tool.name in excluded or tool.name in seen:
                continue
            seen.add(tool.name)
            out.append(tool)

    # 2) MCP client tools are injected by AgentRuntime after BotSpec assembly.
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
    "ToolMaterializationError",
    "build_mcp_tools_schema",
    "build_tools_schema",
    "discover_tools",
    "find_spec",
]
