"""Concrete tool pack catalog shared by BotSpec validation and Agent discovery."""
from __future__ import annotations

import importlib
from typing import Callable

from chatcopilot.contracts.tool_packs import (
    ToolFeatureEntry,
    ToolPackEntry,
    ToolPackPrompt,
)


_BUILTIN_TOOL_PACKS: dict[str, ToolPackEntry] = {
    "feishu.document": ToolPackEntry(
        name="feishu.document",
        manifest_module="chatcopilot.external_tools.feishu.tool_pack_prompts",
        manifest_builder="build_docs_pack",
        tool_modules=("chatcopilot.external_tools.feishu.spec",),
        description="Generic Feishu docx create/append tools (bot identity).",
    ),
    "feishu.sheet": ToolPackEntry(
        name="feishu.sheet",
        manifest_module="chatcopilot.external_tools.feishu.tool_pack_prompts",
        manifest_builder="build_sheets_pack",
        tool_modules=("chatcopilot.external_tools.feishu.spec",),
        description="Generic Feishu spreadsheet read/write/append tools (bot identity).",
    ),
    "feishu.bitable": ToolPackEntry(
        name="feishu.bitable",
        manifest_module="chatcopilot.external_tools.feishu.tool_pack_prompts",
        manifest_builder="build_bitable_pack",
        tool_modules=("chatcopilot.external_tools.feishu.spec",),
        description="Generic Feishu Bitable query/add/update tools (bot identity).",
    ),
    "feishu.wiki": ToolPackEntry(
        name="feishu.wiki",
        manifest_module="chatcopilot.external_tools.feishu.tool_pack_prompts",
        manifest_builder="build_wiki_pack",
        tool_modules=("chatcopilot.external_tools.feishu.spec",),
        description="Generic Feishu wiki / drive search tools (bot identity).",
    ),
    "feishu.messaging": ToolPackEntry(
        name="feishu.messaging",
        manifest_module="chatcopilot.external_tools.feishu.tool_pack_prompts",
        manifest_builder="build_im_pack",
        tool_modules=("chatcopilot.external_tools.feishu.spec",),
        description="Generic Feishu instant-message send tool (bot identity, owner-only).",
    ),
    "filesystem.windows.read": ToolPackEntry(
        name="filesystem.windows.read",
        manifest_module="chatcopilot.external_tools.windows_fs.tool_pack_prompts",
        manifest_builder="build_windows_fs_read_pack",
        tool_modules=("chatcopilot.external_tools.windows_fs.tools",),
        description="Generic Windows / WSL file access tools (win_read_file / win_grep / win_glob).",
    ),
    "unity.codebase.read": ToolPackEntry(
        name="unity.codebase.read",
        manifest_module="chatcopilot.external_tools.unity_codebase.tool_pack_prompts",
        manifest_builder="build_unity_codebase_read_pack",
        tool_modules=("chatcopilot.external_tools.unity_codebase.read_tools",),
        description="Project-aware Unity code retrieval (read / search / glob / find_csharp_symbol).",
    ),
    "unity.skills": ToolPackEntry(
        name="unity.skills",
        manifest_module="chatcopilot.external_tools.unity_codebase.tool_pack_prompts",
        manifest_builder="build_unity_codebase_skills_pack",
        tool_modules=("chatcopilot.external_tools.unity_codebase.skill_tools",),
        description="Wrappers around skill scripts shipped inside each registered Unity project (e.g. path_book).",
    ),
    "codebase.read": ToolPackEntry(
        name="codebase.read",
        manifest_module="chatcopilot.external_tools.codebase.tool_pack_prompts",
        manifest_builder="build_codebase_read_pack",
        tool_modules=("chatcopilot.external_tools.codebase.tools",),
        description="Platform-neutral registered repository inspection (map / search / read).",
    ),
    "dev.files": ToolPackEntry(
        name="dev.files",
        manifest_module="chatcopilot.external_tools.dev.tool_pack_prompts",
        manifest_builder="build_dev_files_pack",
        tool_modules=("chatcopilot.external_tools.dev.file_tools",),
        description="Direct file operations: read, write, edit (fuzzy search-replace), delete, list, search.",
    ),
    "dev.shell": ToolPackEntry(
        name="dev.shell",
        manifest_module="chatcopilot.external_tools.dev.tool_pack_prompts",
        manifest_builder="build_dev_shell_pack",
        tool_modules=("chatcopilot.external_tools.dev.shell_tools",),
        description="Sandboxed shell command execution within the project directory.",
    ),
    "dev.code_tasks": ToolPackEntry(
        name="dev.code_tasks",
        manifest_module="chatcopilot.external_tools.dev.tool_pack_prompts",
        manifest_builder="build_dev_code_tasks_pack",
        tool_modules=(
            "chatcopilot.external_tools.dev.code_task_tools",
            "chatcopilot.external_tools.dev.adapter_tools",
        ),
        description=(
            "Owner-only asynchronous source development in isolated worktrees with "
            "validation, cancellation, resume, and transactional publication."
        ),
    ),
    "career.intelligence": ToolPackEntry(
        name="career.intelligence",
        manifest_module="chatcopilot.external_tools.career.tool_pack_prompts",
        manifest_builder="build_career_intelligence_pack",
        tool_modules=("chatcopilot.external_tools.career.spec",),
        description="AI job discovery, market evidence, and workspace-local intelligence snapshots.",
    ),
    "wiki.knowledge": ToolPackEntry(
        name="wiki.knowledge",
        manifest_module="chatcopilot.external_tools.wiki.tool_pack_prompts",
        manifest_builder="build_wiki_knowledge_pack",
        tool_modules=("chatcopilot.external_tools.wiki.spec",),
        description="Owner-private local Markdown Wiki capture, read, and search tools.",
    ),
    "web.fetch": ToolPackEntry(
        name="web.fetch",
        manifest_module="chatcopilot.external_tools.web_fetch.tool_pack_prompts",
        manifest_builder="build_web_fetch_pack",
        tool_modules=("chatcopilot.external_tools.web_fetch.tools",),
        description="Standalone HTTP page fetch for extracting text from known URLs (no MCP dependency).",
    ),
    "workspace.read_write": ToolPackEntry(
        name="workspace.read_write",
        description="Chat workspace, attachment, and response helpers (builtin).",
    ),
    "memory.chat": ToolPackEntry(
        name="memory.chat",
        description="Chat-local memory read/write helpers (builtin).",
    ),
    "persona.manage": ToolPackEntry(
        name="persona.manage",
        description="Layered persona read (anyone) + owner-only write helpers (builtin).",
    ),
    "playbooks.reader": ToolPackEntry(
        name="playbooks.reader",
        description="Lazy-load registered bot skill bodies via read_bot_skill (builtin).",
    ),
    "mcp.admin": ToolPackEntry(
        name="mcp.admin",
        description="Owner-only MCP discovery, approval, and inventory helpers (builtin).",
    ),
}


_BUILTIN_TOOL_FEATURES: dict[str, ToolFeatureEntry] = {
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


def known_tool_pack_names() -> set[str]:
    return set(_BUILTIN_TOOL_PACKS)


def known_tool_feature_names() -> set[str]:
    return set(_BUILTIN_TOOL_FEATURES)


def get_tool_pack_entry(name: str) -> ToolPackEntry | None:
    return _BUILTIN_TOOL_PACKS.get(name)


def get_tool_feature_entry(name: str) -> ToolFeatureEntry | None:
    return _BUILTIN_TOOL_FEATURES.get(name)


def resolve_tool_modules(tool_pack_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Resolve BotSpec tool pack names into runtime tool module paths."""

    modules: list[str] = []
    seen: set[str] = set()
    for name in tool_pack_names:
        entry = get_tool_pack_entry(name)
        if entry is None:
            continue
        for module in entry.tool_modules:
            if module in seen:
                continue
            seen.add(module)
            modules.append(module)
    return tuple(modules)


def all_tool_modules() -> tuple[str, ...]:
    """Return the ordered union of all built-in tool pack modules."""

    modules: list[str] = []
    seen: set[str] = set()
    for entry in _BUILTIN_TOOL_PACKS.values():
        for module in entry.tool_modules:
            if module in seen:
                continue
            seen.add(module)
            modules.append(module)
    return tuple(modules)


def load_tool_pack_prompt(name: str) -> ToolPackPrompt | None:
    """Load a tool pack prompt guide when it has a Python manifest module."""

    entry = get_tool_pack_entry(name)
    if entry is None or entry.manifest_module is None:
        return None
    module = importlib.import_module(entry.manifest_module)
    build_manifest: Callable[[], ToolPackPrompt] = getattr(module, entry.manifest_builder)
    return build_manifest()


__all__ = [
    "ToolFeatureEntry",
    "ToolPackEntry",
    "ToolPackPrompt",
    "_BUILTIN_TOOL_FEATURES",
    "_BUILTIN_TOOL_PACKS",
    "all_tool_modules",
    "get_tool_feature_entry",
    "get_tool_pack_entry",
    "known_tool_feature_names",
    "known_tool_pack_names",
    "load_tool_pack_prompt",
    "resolve_tool_modules",
]
