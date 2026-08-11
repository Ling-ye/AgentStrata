"""Compatibility views of Agent built-in tool modules.

The concrete pack-to-module mapping belongs to :mod:`chatcopilot.tool_packs.catalog`.
This module derives the legacy views so Agent discovery has no second registry.
"""

from chatcopilot.tool_packs.catalog import get_tool_pack_entry, known_tool_pack_names


_BUILTIN_MODULE_PREFIX = "chatcopilot.agent.tools.builtin."


BUILTIN_TOOL_MODULES_BY_TOOL_PACK = {
    name: tuple(
        module
        for module in entry.tool_modules
        if module.startswith(_BUILTIN_MODULE_PREFIX)
    )
    for name in sorted(known_tool_pack_names())
    if (entry := get_tool_pack_entry(name)) is not None
    and any(module.startswith(_BUILTIN_MODULE_PREFIX) for module in entry.tool_modules)
}

BUILTIN_TOOL_MODULES = tuple(
    dict.fromkeys(
        module
        for modules in BUILTIN_TOOL_MODULES_BY_TOOL_PACK.values()
        for module in modules
    )
)


def resolve_builtin_tool_modules(tool_packs):
    """Resolve built-in tool modules for selected BotSpec tool packs.

    ``None`` keeps the legacy load-all behavior used by MCP compatibility paths.
    An empty sequence means the bot instance runs without built-in tools.
    """

    if tool_packs is None:
        return BUILTIN_TOOL_MODULES
    modules = []
    seen = set()
    for tool_pack in tool_packs:
        for module in BUILTIN_TOOL_MODULES_BY_TOOL_PACK.get(tool_pack, ()):
            if module in seen:
                continue
            seen.add(module)
            modules.append(module)
    return tuple(modules)


__all__ = [
    "BUILTIN_TOOL_MODULES",
    "BUILTIN_TOOL_MODULES_BY_TOOL_PACK",
    "resolve_builtin_tool_modules",
]
