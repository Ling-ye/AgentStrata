"""Stable component catalog API for control surfaces."""

from chatcopilot.component_catalog.catalog import (
    get_mcp_catalog_entry,
    get_tool_feature_entry,
    get_tool_pack_entry,
    iter_mcp_catalog_entries,
    iter_subagent_presets,
    iter_tool_features,
    iter_tool_packs,
    iter_workflows,
)
from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS, PRESET_NAMES

__all__ = [
    "BUILTIN_SUBAGENTS",
    "PRESET_NAMES",
    "get_mcp_catalog_entry",
    "get_tool_feature_entry",
    "get_tool_pack_entry",
    "iter_mcp_catalog_entries",
    "iter_subagent_presets",
    "iter_tool_features",
    "iter_tool_packs",
    "iter_workflows",
]
