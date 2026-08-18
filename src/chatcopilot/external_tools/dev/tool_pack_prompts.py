"""Tool-pack prompt fragments for direct development tools."""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPrompt


def build_dev_files_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="dev.files",
        prompt_fragments=(
            "## File operations\n\n"
            "Use read_file, edit_file, write_file, delete_file, list_directory, and "
            "search_content for direct project file work. Prefer edit_file for a focused "
            "replacement and write_file for a new file or a deliberate full rewrite.",
        ),
    )


def build_dev_shell_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="dev.shell",
        prompt_fragments=(
            "## Shell execution\n\n"
            "Use run_command for tests, validation, and project CLI operations. Respect the "
            "configured project root and command timeout.",
        ),
    )


def build_dev_code_tasks_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="dev.code_tasks",
        prompt_fragments=(
            "## Isolated code tasks\n\n"
            "For any request that requires changing repository source, tests, specs, "
            "documentation, dependencies, BotSpec, adapters, or deployment, call "
            "start_code_task with the complete user intent and observable acceptance "
            "criteria. Return the task id immediately. Use get_code_task to answer progress "
            "questions, cancel_code_task to stop work, and resume_code_task for corrective "
            "follow-up. Do not mutate source directly from the main conversation. If the "
            "user explicitly asks for analysis, design, review, or a plan before later "
            "confirmation, provide the reviewable plan in the current turn and do not call "
            "start_code_task yet. After an explicit confirmation in the same session, call "
            "start_code_task exactly once and restate the complete approved plan and "
            "acceptance criteria; never submit only a short confirmation such as 'proceed'. "
            "A direct request to implement now does not need an extra confirmation turn, "
            "and an isolated confirmation without an unambiguous pending plan must be "
            "clarified.\n\n"
            "For an external open-source adapter, first call prepare_adapter_source and "
            "show its exact envelope and digest to the Owner. Wait for explicit approval "
            "before calling approve_adapter_source. Only then call "
            "forge_open_source_adapter with the unchanged one-shot approval.",
        ),
    )


TOOL_PACK_PROMPT_BUILDERS = {
    "dev.files": build_dev_files_pack,
    "dev.shell": build_dev_shell_pack,
    "dev.code_tasks": build_dev_code_tasks_pack,
}


__all__ = [
    "TOOL_PACK_PROMPT_BUILDERS",
    "build_dev_code_tasks_pack",
    "build_dev_files_pack",
    "build_dev_shell_pack",
]
