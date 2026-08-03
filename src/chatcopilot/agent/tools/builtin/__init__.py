"""Agent built-in tool modules keyed by BotSpec tool pack."""

BUILTIN_TOOL_MODULES_BY_TOOL_PACK = {
    "workspace.read_write": (
        "chatcopilot.agent.tools.builtin.workspace_tools",
    ),
    "memory.chat": (
        "chatcopilot.agent.tools.builtin.memory_tools",
    ),
    "persona.manage": (
        "chatcopilot.agent.tools.builtin.persona_tools",
    ),
    "playbooks.reader": (
        "chatcopilot.agent.tools.builtin.skill_tools",
    ),
    "mcp.admin": (
        "chatcopilot.external_tools.mcp_admin.tools",
    ),
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
