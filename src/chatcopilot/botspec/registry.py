"""BotSpec discovery helpers backed by the canonical component catalog."""
from __future__ import annotations


from chatcopilot.core.bot_paths import resolve_bot_spec_path
from chatcopilot.tool_packs.catalog import (
    ToolFeatureEntry,
    ToolPackEntry,
    ToolPackPolicy,
    BUILTIN_TOOL_FEATURES,
    BUILTIN_TOOL_PACKS,
    all_tool_modules,
    get_tool_feature_entry,
    get_tool_pack_entry,
    known_tool_feature_names,
    known_tool_pack_names,
    load_tool_pack_policies,
    resolve_tool_modules,
)

_BUILTIN_TOOL_FEATURES = BUILTIN_TOOL_FEATURES
_BUILTIN_TOOL_PACKS = BUILTIN_TOOL_PACKS


__all__ = [
    "ToolFeatureEntry",
    "ToolPackEntry",
    "ToolPackPolicy",
    "_BUILTIN_TOOL_FEATURES",
    "_BUILTIN_TOOL_PACKS",
    "all_tool_modules",
    "get_tool_feature_entry",
    "get_tool_pack_entry",
    "known_tool_feature_names",
    "known_tool_pack_names",
    "load_tool_pack_policies",
    "resolve_bot_spec_path",
    "resolve_tool_modules",
]
