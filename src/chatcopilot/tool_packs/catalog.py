"""Explicit provider-module index shared by BotSpec and tool discovery.

Tool membership belongs to the provider exported by each domain module.  This
index deliberately contains no exact tool-name list.
"""
from __future__ import annotations

import importlib
from types import MappingProxyType
from typing import Callable, Mapping

from chatcopilot.contracts.tool_packs import (
    ToolFeatureEntry,
    ToolPackEntry,
    ToolPackPolicy,
)


def _entry(
    name: str,
    provider_module: str | None,
    description: str,
    *,
    dynamic: bool = False,
    policy_module: str | None = None,
    policy_builder: str = "build_policy",
) -> ToolPackEntry:
    return ToolPackEntry(
        name=name,
        provider_module=provider_module,
        dynamic=dynamic,
        policy_module=policy_module,
        policy_builder=policy_builder,
        description=description,
    )


_BUILTIN_TOOL_PACKS_DATA: dict[str, ToolPackEntry] = {
    "feishu.document": _entry(
        "feishu.document",
        "chatcopilot.external_tools.feishu",
        "Generic Feishu docx create/append tools (bot identity).",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_docs_pack",
    ),
    "feishu.sheet": _entry(
        "feishu.sheet",
        "chatcopilot.external_tools.feishu",
        "Generic Feishu spreadsheet read/write/append tools (bot identity).",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_sheets_pack",
    ),
    "feishu.bitable": _entry(
        "feishu.bitable",
        "chatcopilot.external_tools.feishu",
        "Generic Feishu Bitable query/add/update tools (bot identity).",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_bitable_pack",
    ),
    "feishu.wiki": _entry(
        "feishu.wiki",
        "chatcopilot.external_tools.feishu",
        "Generic Feishu wiki and drive search tools (bot identity).",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_wiki_pack",
    ),
    "feishu.messaging": _entry(
        "feishu.messaging",
        "chatcopilot.external_tools.feishu",
        "Generic Feishu instant-message tools (bot identity, Owner-only).",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_im_pack",
    ),
    "filesystem.windows.read": _entry(
        "filesystem.windows.read",
        "chatcopilot.external_tools.windows_fs.tools",
        "Generic Windows and WSL read-only filesystem tools.",
        policy_module="chatcopilot.external_tools.windows_fs.tool_pack_policies",
        policy_builder="build_windows_fs_read_pack",
    ),
    "unity.codebase.read": _entry(
        "unity.codebase.read",
        "chatcopilot.external_tools.unity_codebase",
        "Project-aware Unity code retrieval tools.",
        policy_module="chatcopilot.external_tools.unity_codebase.tool_pack_policies",
        policy_builder="build_unity_codebase_read_pack",
    ),
    "unity.skills": _entry(
        "unity.skills",
        "chatcopilot.external_tools.unity_codebase",
        "Wrappers around registered Unity-project skill scripts.",
        policy_module="chatcopilot.external_tools.unity_codebase.tool_pack_policies",
        policy_builder="build_unity_codebase_skills_pack",
    ),
    "codebase.read": _entry(
        "codebase.read",
        "chatcopilot.external_tools.codebase.tools",
        "Platform-neutral registered repository inspection tools.",
        policy_module="chatcopilot.external_tools.codebase.tool_pack_policies",
        policy_builder="build_codebase_read_pack",
    ),
    "dev.files": _entry(
        "dev.files",
        "chatcopilot.external_tools.dev",
        "Direct project file operation tools.",
        policy_module="chatcopilot.external_tools.dev.tool_pack_policies",
        policy_builder="build_dev_files_pack",
    ),
    "dev.shell": _entry(
        "dev.shell",
        "chatcopilot.external_tools.dev",
        "Sandboxed project shell command execution.",
        policy_module="chatcopilot.external_tools.dev.tool_pack_policies",
        policy_builder="build_dev_shell_pack",
    ),
    "dev.code_tasks": _entry(
        "dev.code_tasks",
        "chatcopilot.external_tools.dev",
        "Owner-only isolated source-development task tools.",
        policy_module="chatcopilot.external_tools.dev.tool_pack_policies",
        policy_builder="build_dev_code_tasks_pack",
    ),
    "career.intelligence": _entry(
        "career.intelligence",
        "chatcopilot.external_tools.career.spec",
        "Job discovery and workspace-local career intelligence tools.",
        policy_module="chatcopilot.external_tools.career.tool_pack_policies",
        policy_builder="build_career_intelligence_pack",
    ),
    "wiki.knowledge": _entry(
        "wiki.knowledge",
        "chatcopilot.external_tools.wiki.spec",
        "Owner-private Markdown Wiki capture and retrieval tools.",
        policy_module="chatcopilot.external_tools.wiki.tool_pack_policies",
        policy_builder="build_wiki_knowledge_pack",
    ),
    "web.fetch": _entry(
        "web.fetch",
        "chatcopilot.external_tools.web_fetch.tools",
        "HTTP page fetch for extracting text from known URLs.",
        policy_module="chatcopilot.external_tools.web_fetch.tool_pack_policies",
        policy_builder="build_web_fetch_pack",
    ),
    "workspace.read_write": _entry(
        "workspace.read_write",
        "chatcopilot.agent.tools.builtin.workspace_tools",
        "Chat workspace, attachment, and response helper tools.",
    ),
    "memory.chat": _entry(
        "memory.chat",
        "chatcopilot.agent.tools.builtin.memory_tools",
        "Trusted conversation-scoped memory tools.",
    ),
    "playbooks.reader": _entry(
        "playbooks.reader",
        "chatcopilot.agent.tools.builtin.skill_tools",
        "Lazy loading for registered bot playbooks.",
    ),
    "mcp.admin": _entry(
        "mcp.admin",
        "chatcopilot.external_tools.mcp_admin.tools",
        "Owner-only MCP discovery, approval, and inventory tools.",
    ),
    "mcp.dynamic": _entry(
        "mcp.dynamic",
        "chatcopilot.agent.mcp.client",
        "Main-Agent tools materialized from configured MCP servers.",
        dynamic=True,
    ),
    "mcp.subagent": _entry(
        "mcp.subagent",
        "chatcopilot.agent.mcp.client",
        "Subagent-only tools materialized from configured MCP servers.",
        dynamic=True,
    ),
    "search.unified": _entry(
        "search.unified",
        "chatcopilot.agent.search.tool",
        "Session-bound unified search tool.",
        dynamic=True,
    ),
    "agent.delegation": _entry(
        "agent.delegation",
        "chatcopilot.agent.subagents.registry",
        "Session-bound subagent and workflow delegation tools.",
        dynamic=True,
    ),
    "persona.control": _entry(
        "persona.control",
        "chatcopilot.agent.persona.tools",
        "Owner-only session-bound persona management tool.",
        dynamic=True,
    ),
    "runtime.session": _entry(
        "runtime.session",
        None,
        "Adapter-supplied session-local control tools.",
        dynamic=True,
    ),
}


_BUILTIN_TOOL_FEATURES_DATA: dict[str, ToolFeatureEntry] = {
    "chat.file_uploads": ToolFeatureEntry(
        name="chat.file_uploads",
        description="Deterministic user file upload storage feature.",
    ),
    "chat.image_inputs": ToolFeatureEntry(
        name="chat.image_inputs",
        description="Validated multimodal image input for vision-capable agent backends.",
    ),
    "chat.private_workspace": ToolFeatureEntry(
        name="chat.private_workspace",
        description="Deterministic private workspace inventory feature.",
    ),
}

BUILTIN_TOOL_PACKS: Mapping[str, ToolPackEntry] = MappingProxyType(
    _BUILTIN_TOOL_PACKS_DATA
)
BUILTIN_TOOL_FEATURES: Mapping[str, ToolFeatureEntry] = MappingProxyType(
    _BUILTIN_TOOL_FEATURES_DATA
)


def known_tool_pack_names() -> set[str]:
    return set(BUILTIN_TOOL_PACKS)


def known_tool_feature_names() -> set[str]:
    return set(BUILTIN_TOOL_FEATURES)


def get_tool_pack_entry(name: str) -> ToolPackEntry | None:
    return BUILTIN_TOOL_PACKS.get(name)


def get_tool_feature_entry(name: str) -> ToolFeatureEntry | None:
    return BUILTIN_TOOL_FEATURES.get(name)


def resolve_tool_modules(tool_pack_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Resolve selected packs to their unique provider modules."""

    return tuple(
        dict.fromkeys(
            entry.provider_module
            for name in tool_pack_names
            if (entry := get_tool_pack_entry(name)) is not None
            and entry.provider_module is not None
        )
    )


def all_tool_modules() -> tuple[str, ...]:
    return resolve_tool_modules(list(BUILTIN_TOOL_PACKS))


def load_tool_pack_policies(name: str) -> tuple[ToolPackPolicy, ...]:
    """Load stable cross-tool policies declared by one catalog entry."""

    entry = get_tool_pack_entry(name)
    if entry is None or entry.policy_module is None:
        return ()
    module = importlib.import_module(entry.policy_module)
    build_policy: Callable[[], tuple[ToolPackPolicy, ...]] = getattr(
        module, entry.policy_builder
    )
    return build_policy()


_BUILTIN_TOOL_FEATURES = BUILTIN_TOOL_FEATURES
_BUILTIN_TOOL_PACKS = BUILTIN_TOOL_PACKS


__all__ = [
    "BUILTIN_TOOL_FEATURES",
    "BUILTIN_TOOL_PACKS",
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
    "resolve_tool_modules",
]
