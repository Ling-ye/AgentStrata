"""Stable read-only component catalog API.

This module lets control surfaces inspect built-in tool packs, tool features,
MCP catalog entries, subagent presets, and workflows without importing Agent or
BotSpec runtime internals.
"""
from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import ModuleType
from typing import Callable

from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS
from chatcopilot.contracts.subagents import (
    BUILTIN_SUBAGENT_WORKFLOWS,
    SubagentDef,
    WorkflowDef,
)
from chatcopilot.contracts.tool_packs import ToolFeatureEntry, ToolPackEntry
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.core.mcp_catalog import McpCatalogEntry, load_mcp_catalog
from chatcopilot.tool_packs.catalog import (
    get_tool_feature_entry,
    get_tool_pack_entry,
    known_tool_feature_names,
    known_tool_pack_names,
    resolve_tool_bindings,
)


ModuleLoader = Callable[[str], ModuleType]


class CatalogProjectionError(RuntimeError):
    """A declared catalog projection cannot be materialized exactly."""


def iter_tool_packs() -> Iterator[tuple[str, ToolPackEntry]]:
    for name in sorted(known_tool_pack_names()):
        entry = get_tool_pack_entry(name)
        if entry is not None:
            yield name, entry


def iter_tool_features() -> Iterator[tuple[str, ToolFeatureEntry]]:
    for name in sorted(known_tool_feature_names()):
        entry = get_tool_feature_entry(name)
        if entry is not None:
            yield name, entry


def iter_tool_pack_tools(
    name: str,
    *,
    module_loader: ModuleLoader = importlib.import_module,
) -> Iterator[ToolDef]:
    """Yield the exact ordered ToolDef projection declared by one pack."""

    seen: set[str] = set()
    for binding in resolve_tool_bindings((name,)):
        try:
            module = module_loader(binding.module)
        except Exception as exc:  # noqa: BLE001
            raise CatalogProjectionError(
                f"tool module could not be imported: {binding.module} ({type(exc).__name__})"
            ) from None
        exported = getattr(module, "TOOLS", None)
        if not isinstance(exported, (list, tuple)):
            raise CatalogProjectionError(
                f"tool module must export a list or tuple named TOOLS: {binding.module}"
            )
        by_name: dict[str, ToolDef] = {}
        for tool in exported:
            if not isinstance(tool, ToolDef):
                continue
            if tool.name in by_name:
                raise CatalogProjectionError(
                    f"tool module exports duplicate tool name {tool.name!r}: {binding.module}"
                )
            by_name[tool.name] = tool
        for tool_name in binding.tool_names:
            tool = by_name.get(tool_name)
            if tool is None:
                raise CatalogProjectionError(
                    f"declared tool {tool_name!r} is missing from {binding.module}"
                )
            if tool_name in seen:
                continue
            seen.add(tool_name)
            yield tool


def get_mcp_catalog_entry(ref: str) -> McpCatalogEntry | None:
    return load_mcp_catalog().get(ref)


def iter_mcp_catalog_entries(
    *,
    use_env_override: bool = True,
    strict: bool = False,
) -> Iterator[tuple[str, McpCatalogEntry]]:
    yield from sorted(
        load_mcp_catalog(
            use_env_override=use_env_override,
            strict=strict,
        ).items()
    )


def iter_subagent_presets() -> Iterator[tuple[str, SubagentDef]]:
    yield from sorted(BUILTIN_SUBAGENTS.items())


def iter_workflows() -> Iterator[tuple[str, WorkflowDef]]:
    yield from sorted(BUILTIN_SUBAGENT_WORKFLOWS.items())


__all__ = [
    "CatalogProjectionError",
    "get_tool_feature_entry",
    "get_tool_pack_entry",
    "get_mcp_catalog_entry",
    "iter_mcp_catalog_entries",
    "iter_subagent_presets",
    "iter_tool_features",
    "iter_tool_pack_tools",
    "iter_tool_packs",
    "iter_workflows",
]
