"""Concrete tool pack catalog shared by BotSpec validation and Agent discovery."""
from __future__ import annotations

import importlib
from types import MappingProxyType
from typing import Callable, Mapping

from chatcopilot.contracts.tool_packs import (
    ToolFeatureEntry,
    ToolModuleBinding,
    ToolPackEntry,
    ToolPackPolicy,
)


def _binding(module: str, *tool_names: str) -> ToolModuleBinding:
    return ToolModuleBinding(module=module, tool_names=tuple(tool_names))


_BUILTIN_TOOL_PACKS_DATA: dict[str, ToolPackEntry] = {
    "feishu.document": ToolPackEntry(
        name="feishu.document",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_docs_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.feishu.spec",
                "feishu_doc_create",
                "feishu_doc_append",
                "feishu_api_get",
            ),
        ),
        description="Generic Feishu docx create/append tools (bot identity).",
    ),
    "feishu.sheet": ToolPackEntry(
        name="feishu.sheet",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_sheets_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.feishu.spec",
                "feishu_sheet_read",
                "feishu_sheet_write",
                "feishu_sheet_append",
                "feishu_api_get",
            ),
        ),
        description="Generic Feishu spreadsheet read/write/append tools (bot identity).",
    ),
    "feishu.bitable": ToolPackEntry(
        name="feishu.bitable",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_bitable_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.feishu.spec",
                "feishu_bitable_query",
                "feishu_bitable_add",
                "feishu_bitable_update",
                "feishu_api_get",
            ),
        ),
        description="Generic Feishu Bitable query/add/update tools (bot identity).",
    ),
    "feishu.wiki": ToolPackEntry(
        name="feishu.wiki",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_wiki_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.feishu.spec",
                "feishu_wiki_search",
                "feishu_drive_search",
                "feishu_api_get",
            ),
        ),
        description="Generic Feishu wiki / drive search tools (bot identity).",
    ),
    "feishu.messaging": ToolPackEntry(
        name="feishu.messaging",
        policy_module="chatcopilot.external_tools.feishu.tool_pack_policies",
        policy_builder="build_im_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.feishu.spec",
                "feishu_im_send",
                "feishu_api_get",
            ),
        ),
        description="Generic Feishu instant-message send tool (bot identity, owner-only).",
    ),
    "filesystem.windows.read": ToolPackEntry(
        name="filesystem.windows.read",
        policy_module="chatcopilot.external_tools.windows_fs.tool_pack_policies",
        policy_builder="build_windows_fs_read_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.windows_fs.tools",
                "win_read_file",
                "win_grep",
                "win_glob",
            ),
        ),
        description="Generic Windows / WSL file access tools (win_read_file / win_grep / win_glob).",
    ),
    "unity.codebase.read": ToolPackEntry(
        name="unity.codebase.read",
        policy_module="chatcopilot.external_tools.unity_codebase.tool_pack_policies",
        policy_builder="build_unity_codebase_read_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.unity_codebase.read_tools",
                "unity_project_read",
                "unity_project_search",
                "unity_project_glob",
                "unity_find_csharp_symbol",
            ),
        ),
        description="Project-aware Unity code retrieval (read / search / glob / find_csharp_symbol).",
    ),
    "unity.skills": ToolPackEntry(
        name="unity.skills",
        policy_module="chatcopilot.external_tools.unity_codebase.tool_pack_policies",
        policy_builder="build_unity_codebase_skills_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.unity_codebase.skill_tools",
                "unity_path_book",
            ),
        ),
        description="Wrappers around skill scripts shipped inside each registered Unity project (e.g. path_book).",
    ),
    "codebase.read": ToolPackEntry(
        name="codebase.read",
        policy_module="chatcopilot.external_tools.codebase.tool_pack_policies",
        policy_builder="build_codebase_read_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.codebase.tools",
                "codebase_list_repositories",
                "codebase_map",
                "codebase_search",
                "codebase_symbols",
                "codebase_read",
                "codebase_references",
                "codebase_dependencies",
                "codebase_context",
            ),
        ),
        description="Platform-neutral registered repository inspection (map / search / read).",
    ),
    "dev.files": ToolPackEntry(
        name="dev.files",
        policy_module="chatcopilot.external_tools.dev.tool_pack_policies",
        policy_builder="build_dev_files_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.dev.file_tools",
                "read_file",
                "write_file",
                "edit_file",
                "delete_file",
                "list_directory",
                "search_content",
            ),
        ),
        description="Direct file operations: read, write, edit (fuzzy search-replace), delete, list, search.",
    ),
    "dev.shell": ToolPackEntry(
        name="dev.shell",
        policy_module="chatcopilot.external_tools.dev.tool_pack_policies",
        policy_builder="build_dev_shell_pack",
        tool_bindings=(
            _binding("chatcopilot.external_tools.dev.shell_tools", "run_command"),
        ),
        description="Sandboxed shell command execution within the project directory.",
    ),
    "dev.code_tasks": ToolPackEntry(
        name="dev.code_tasks",
        policy_module="chatcopilot.external_tools.dev.tool_pack_policies",
        policy_builder="build_dev_code_tasks_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.dev.code_task_tools",
                "start_code_task",
                "get_code_task",
                "cancel_code_task",
                "resume_code_task",
            ),
            _binding(
                "chatcopilot.external_tools.dev.adapter_tools",
                "prepare_adapter_source",
                "approve_adapter_source",
            ),
        ),
        description=(
            "Owner-only asynchronous source development in isolated worktrees with "
            "validation, cancellation, resume, and transactional publication."
        ),
    ),
    "career.intelligence": ToolPackEntry(
        name="career.intelligence",
        policy_module="chatcopilot.external_tools.career.tool_pack_policies",
        policy_builder="build_career_intelligence_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.career.spec",
                "career_watchlist_update",
                "career_watchlist_show",
                "search_company_ai_jobs",
                "career_jobs_ingest",
                "career_intel_ingest",
                "career_intel_query",
            ),
        ),
        description="AI job discovery, market evidence, and workspace-local intelligence snapshots.",
    ),
    "wiki.knowledge": ToolPackEntry(
        name="wiki.knowledge",
        policy_module="chatcopilot.external_tools.wiki.tool_pack_policies",
        policy_builder="build_wiki_knowledge_pack",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.wiki.spec",
                "wiki_upsert_page",
                "wiki_search",
                "wiki_read_page",
                "wiki_list_pages",
            ),
        ),
        description="Owner-private local Markdown Wiki capture, read, and search tools.",
    ),
    "web.fetch": ToolPackEntry(
        name="web.fetch",
        policy_module="chatcopilot.external_tools.web_fetch.tool_pack_policies",
        policy_builder="build_web_fetch_pack",
        tool_bindings=(
            _binding("chatcopilot.external_tools.web_fetch.tools", "web_fetch_page"),
        ),
        description="Standalone HTTP page fetch for extracting text from known URLs (no MCP dependency).",
    ),
    "workspace.read_write": ToolPackEntry(
        name="workspace.read_write",
        tool_bindings=(
            _binding(
                "chatcopilot.agent.tools.builtin.workspace_tools",
                "list_workspace",
                "get_job_status",
                "get_task_status",
                "read_text_head",
                "unzip_attachment",
                "send_files_to_user",
                "download_image_urls",
                "owner_list_workspaces",
                "owner_read_workspace_file",
            ),
        ),
        description="Chat workspace, attachment, and response helpers (builtin).",
    ),
    "memory.chat": ToolPackEntry(
        name="memory.chat",
        tool_bindings=(
            _binding(
                "chatcopilot.agent.tools.builtin.memory_tools",
                "read_memory",
                "append_memory",
                "clear_memory",
            ),
        ),
        description=(
            "Trusted conversation-scoped memory helpers: admitted users read/append; "
            "private users clear self and only Owner clears group memory."
        ),
    ),
    "playbooks.reader": ToolPackEntry(
        name="playbooks.reader",
        tool_bindings=(
            _binding(
                "chatcopilot.agent.tools.builtin.skill_tools",
                "read_bot_skill",
            ),
        ),
        description="Lazy-load registered bot skill bodies via read_bot_skill (builtin).",
    ),
    "mcp.admin": ToolPackEntry(
        name="mcp.admin",
        tool_bindings=(
            _binding(
                "chatcopilot.external_tools.mcp_admin.tools",
                "discover_mcp_server",
                "approve_mcp_server",
                "probe_mcp_server",
                "list_mcp_servers",
            ),
        ),
        description="Owner-only MCP discovery, approval, and inventory helpers (builtin).",
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


def resolve_tool_bindings(
    tool_pack_names: tuple[str, ...] | list[str],
) -> tuple[ToolModuleBinding, ...]:
    """Resolve packs into an ordered union of exact module/tool bindings."""

    names_by_module: dict[str, list[str]] = {}
    seen_by_module: dict[str, set[str]] = {}
    for name in tool_pack_names:
        entry = get_tool_pack_entry(name)
        if entry is None:
            continue
        for binding in entry.tool_bindings:
            names = names_by_module.setdefault(binding.module, [])
            seen = seen_by_module.setdefault(binding.module, set())
            for tool_name in binding.tool_names:
                if tool_name in seen:
                    continue
                seen.add(tool_name)
                names.append(tool_name)
    return tuple(
        ToolModuleBinding(module=module, tool_names=tuple(tool_names))
        for module, tool_names in names_by_module.items()
    )


def resolve_tool_modules(tool_pack_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Resolve BotSpec tool pack names into runtime tool module paths."""

    return tuple(binding.module for binding in resolve_tool_bindings(tool_pack_names))


def all_tool_bindings() -> tuple[ToolModuleBinding, ...]:
    """Return the exact bindings for the complete built-in catalog."""

    return resolve_tool_bindings(list(BUILTIN_TOOL_PACKS))


def all_tool_modules() -> tuple[str, ...]:
    """Return the ordered union of all built-in tool pack modules."""

    return tuple(binding.module for binding in all_tool_bindings())


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
    "ToolModuleBinding",
    "ToolPackEntry",
    "ToolPackPolicy",
    "_BUILTIN_TOOL_FEATURES",
    "_BUILTIN_TOOL_PACKS",
    "all_tool_bindings",
    "all_tool_modules",
    "get_tool_feature_entry",
    "get_tool_pack_entry",
    "known_tool_feature_names",
    "known_tool_pack_names",
    "load_tool_pack_policies",
    "resolve_tool_bindings",
    "resolve_tool_modules",
]
