"""Stable read-only component catalog API.

This module lets control surfaces inspect built-in tool packs, tool features,
MCP catalog entries, subagent presets, and workflows without importing Agent or
BotSpec runtime internals.
"""
from __future__ import annotations

from collections.abc import Iterator

from chatcopilot.contracts.subagents import BUILTIN_SUBAGENT_WORKFLOWS, WorkflowDef
from chatcopilot.core.mcp_catalog import McpCatalogEntry, load_mcp_catalog
from chatcopilot.contracts.tool_packs import ToolFeatureEntry, ToolPackEntry
from chatcopilot.tool_packs.catalog import (
    get_tool_feature_entry,
    get_tool_pack_entry,
    known_tool_feature_names,
    known_tool_pack_names,
)
from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS
from chatcopilot.contracts.subagents import SubagentDef


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


def get_mcp_catalog_entry(ref: str) -> McpCatalogEntry | None:
    return load_mcp_catalog().get(ref)


def iter_mcp_catalog_entries() -> Iterator[tuple[str, McpCatalogEntry]]:
    yield from sorted(load_mcp_catalog().items())


def iter_subagent_presets() -> Iterator[tuple[str, SubagentDef]]:
    yield from sorted(BUILTIN_SUBAGENTS.items())


def iter_workflows() -> Iterator[tuple[str, WorkflowDef]]:
    yield from sorted(BUILTIN_SUBAGENT_WORKFLOWS.items())


__all__ = [
    "get_tool_feature_entry",
    "get_tool_pack_entry",
    "get_mcp_catalog_entry",
    "iter_mcp_catalog_entries",
    "iter_subagent_presets",
    "iter_tool_features",
    "iter_tool_packs",
    "iter_workflows",
]
