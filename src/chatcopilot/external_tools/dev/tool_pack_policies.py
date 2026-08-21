"""Structured cross-tool policies for development tool packs."""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPolicy, tool_pack_policies


def build_dev_files_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies(
        "dev.files",
            "## File operations\n\n"
            "Use read_file, edit_file, write_file, delete_file, list_directory, and "
            "search_content for direct project file work. Prefer edit_file for a focused "
            "replacement and write_file for a new file or a deliberate full rewrite.",
    )


def build_dev_shell_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies(
        "dev.shell",
            "## Shell execution\n\n"
            "Use run_command for tests, validation, and project CLI operations. Respect the "
            "configured project root and command timeout.",
    )


def build_dev_code_tasks_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies(
        "dev.code_tasks",
        "Repository changes run only in an isolated code task. A direct implementation "
        "request submits one complete objective and observable acceptance criteria; a "
        "plan-only request waits for explicit approval, then submits the entire approved "
        "plan exactly once. Progress, cancellation, and corrective continuation stay "
        "bound to the returned task id. External adapter import additionally requires a "
        "reviewed source envelope and digest, an unchanged one-shot Owner approval, and "
        "only then the forge step.",
        applies_to_roles=("owner",),
    )


TOOL_PACK_POLICY_BUILDERS = {
    "dev.files": build_dev_files_pack,
    "dev.shell": build_dev_shell_pack,
    "dev.code_tasks": build_dev_code_tasks_pack,
}


__all__ = [
    "TOOL_PACK_POLICY_BUILDERS",
    "build_dev_code_tasks_pack",
    "build_dev_files_pack",
    "build_dev_shell_pack",
]
