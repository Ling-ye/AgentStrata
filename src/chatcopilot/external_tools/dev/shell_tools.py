"""Dev shell tool: sandboxed command execution within the project directory."""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from chatcopilot.contracts.development import current_development_task_scope
from chatcopilot.external_tools.dev.config import get_dev_config, DevConfig
from chatcopilot.external_tools.dev.path_guard import DevPathAccessError
from chatcopilot.external_tools.shared.tool_spec import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)


def _validate_command(config: DevConfig, command: str) -> str | None:
    """Return error message if command matches a blocked pattern, else None."""
    for pattern in config.shell.blocked_patterns:
        if pattern.search(command):
            return f"Command blocked by safety rule: {pattern.pattern}"
    scope = current_development_task_scope()
    if scope is not None and scope.shell_profile == "validation":
        return _validate_validation_command(command)
    return None


_SHELL_CONTROL_RE = re.compile(r"(?:&&|\|\||[|;<>`]|\$\(|\r|\n)")
_PYTHON_MODULES = frozenset({"pytest", "compileall"})
_PYTHON_SCRIPTS = frozenset(
    {
        "scripts/check_architecture.py",
        "scripts/check_sdd_specs.py",
    }
)
_DIRECT_VALIDATORS = frozenset({"ruff", "mypy", "pyright"})


def _validate_validation_command(command: str) -> str | None:
    """Restrict delegated shell use to deterministic validation/read commands."""

    if _SHELL_CONTROL_RE.search(command):
        return "Command blocked by delegated validation profile: shell operators are not allowed"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return f"Command blocked by delegated validation profile: {exc}"
    if not argv:
        return "Command blocked by delegated validation profile: empty command"

    executable = Path(argv[0]).name
    if executable in {"python", "python3"} or argv[0].endswith("/bin/python"):
        return _validate_python_command(argv)
    if executable == "git":
        return _validate_git_command(argv)
    if executable == "npm":
        if len(argv) >= 3 and argv[1] == "run" and argv[2] in {
            "build",
            "check",
            "lint",
            "test",
            "typecheck",
        }:
            return None
    if executable == "cargo" and len(argv) >= 2 and argv[1] in {
        "check",
        "clippy",
        "test",
    }:
        return None
    if executable == "go" and len(argv) >= 2 and argv[1] in {"test", "vet"}:
        return None
    if executable in _DIRECT_VALIDATORS:
        return None
    if executable == "rg":
        return None
    return (
        "Command blocked by delegated validation profile: only tests, builds, linters, "
        "read-only git, and repository search are allowed"
    )


def _validate_python_command(argv: list[str]) -> str | None:
    if len(argv) >= 3 and argv[1] == "-m":
        module = argv[2]
        if module in _PYTHON_MODULES:
            return None
        if module == "chatcopilot" and argv[3:5] == ["botspec", "validate"]:
            return None
    if len(argv) >= 2 and argv[1].replace("\\", "/") in _PYTHON_SCRIPTS:
        return None
    return (
        "Command blocked by delegated validation profile: Python may run pytest, "
        "compileall, BotSpec validation, or checked repository validators only"
    )


def _validate_git_command(argv: list[str]) -> str | None:
    if len(argv) < 2:
        return "Command blocked by delegated validation profile: git subcommand is required"
    if argv[1] in {"diff", "grep", "log", "ls-files", "rev-parse", "show", "status"}:
        return None
    return "Command blocked by delegated validation profile: git command is not read-only"


def _resolve_cwd(config: DevConfig, cwd_raw: str | None) -> Path:
    """Resolve and validate working directory."""
    if not cwd_raw or not cwd_raw.strip():
        return config.repo_root

    target = Path(cwd_raw.strip())
    if not target.is_absolute():
        target = config.repo_root / target

    resolved = target.resolve()
    try:
        resolved.relative_to(config.repo_root)
    except ValueError:
        raise DevPathAccessError(
            f"cwd must be within project root: {cwd_raw}"
        )
    return resolved


def _handle_run_command(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    config = get_dev_config()
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolResult(
            ok=False,
            error="command is required",
            error_code="command_required",
            stage="validation",
        )

    blocked = _validate_command(config, command)
    if blocked:
        return ToolResult(
            ok=False,
            error=blocked,
            error_code="command_blocked",
            stage="validation",
        )

    cwd_raw = args.get("cwd")
    try:
        cwd = _resolve_cwd(config, str(cwd_raw) if cwd_raw else None)
    except DevPathAccessError as e:
        return ToolResult(
            ok=False,
            error=str(e),
            error_code="command_cwd_invalid",
            stage="validation",
        )

    timeout_raw = args.get("timeout_seconds")
    timeout = config.shell.timeout_default
    if timeout_raw is not None:
        timeout = min(int(timeout_raw), config.shell.timeout_max)
        timeout = max(1, timeout)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
            env=None,  # inherit parent env
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            ok=False,
            error=f"Command timed out after {timeout}s: {command}",
            error_code="command_timeout",
            stage="execution",
        )
    except OSError as e:
        return ToolResult(
            ok=False,
            error=f"Command execution failed: {e}",
            error_code="command_execution_failed",
            stage="execution",
        )

    output_parts: list[str] = []
    if result.stdout:
        output_parts.append(result.stdout)
    if result.stderr:
        output_parts.append(f"[stderr]\n{result.stderr}")

    combined = "\n".join(output_parts)
    max_chars = config.shell.max_output_chars
    if len(combined) > max_chars:
        combined = combined[:max_chars] + f"\n\n[output truncated at {max_chars} chars]"

    exit_code = result.returncode
    status = "OK" if exit_code == 0 else f"exit code {exit_code}"
    summary = f"[{status}] {command}"

    return ToolResult(
        ok=exit_code == 0,
        summary=summary if exit_code == 0 else "",
        data={"command": command, "exit_code": exit_code, "output": combined},
        error=summary if exit_code != 0 else None,
        error_code="command_nonzero_exit" if exit_code != 0 else "",
        stage="execution" if exit_code != 0 else "",
    )


TOOLS: list[ToolDef] = [
    ToolDef(
        name="run_command",
        summary=(
            "Execute a shell command in the project directory. "
            "Use for running tests, builds, linters, or other dev tasks. "
            "Commands are restricted to the project root."
        ),
        input_schema=object_schema({
            "command": {"type": "string", "description": "Shell command to execute"},
            "cwd": {"type": "string", "description": "Working directory relative to project root (default: project root)"},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)"},
        }, required=("command",)),
        output_schema=object_schema(
            {
                "command": {"type": "string"},
                "exit_code": {"type": "integer"},
                "output": {"type": "string"},
            },
            required=("command", "exit_code", "output"),
        ),
        handler=_handle_run_command,
        category="dev.shell",
        owner="dev",
        module=__name__,
        requires_role="owner",
        weight="heavy",
    ),
]
