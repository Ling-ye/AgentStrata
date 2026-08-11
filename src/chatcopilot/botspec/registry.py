"""BotSpec discovery helpers and compatibility exports for tool pack catalog."""
from __future__ import annotations


from chatcopilot.core.bot_paths import resolve_bot_spec_path
from chatcopilot.tool_packs.catalog import (
    ToolFeatureEntry,
    ToolModuleBinding,
    ToolPackEntry,
    ToolPackPrompt,
    _BUILTIN_TOOL_FEATURES,
    _BUILTIN_TOOL_PACKS,
    all_tool_bindings,
    all_tool_modules,
    get_tool_feature_entry,
    get_tool_pack_entry,
    known_tool_feature_names,
    known_tool_pack_names,
    load_tool_pack_prompt,
    resolve_tool_bindings,
    resolve_tool_modules,
)


__all__ = [
    "ToolFeatureEntry",
    "ToolModuleBinding",
    "ToolPackEntry",
    "ToolPackPrompt",
    "_BUILTIN_TOOL_FEATURES",
    "_BUILTIN_TOOL_PACKS",
    "all_tool_bindings",
    "all_tool_modules",
    "get_tool_feature_entry",
    "get_tool_pack_entry",
    "known_tool_feature_names",
    "known_tool_pack_names",
    "load_tool_pack_prompt",
    "resolve_bot_spec_path",
    "resolve_tool_bindings",
    "resolve_tool_modules",
]
