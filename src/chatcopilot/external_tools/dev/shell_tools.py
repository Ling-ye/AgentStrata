"""Dev shell tool: sandboxed command execution within the project directory."""
from __future__ import annotations

import subprocess
import tempfile
from uuid import uuid4
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.core.scoped_process import sandbox_command
from chatcopilot.core.scoped_files import write_stream
from chatcopilot.external_tools.dev.command_policy import command_argv
from chatcopilot.contracts.development import current_development_task_scope
from chatcopilot.external_tools.dev.config import get_dev_config, DevConfig
from chatcopilot.external_tools.dev.path_guard import DevPathAccessError
from chatcopilot.external_tools.shared.tool_spec import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)


def _resolve_cwd(config: DevConfig, cwd_raw: str | None) -> Path:
    """Resolve and validate working directory."""
    if not cwd_raw or not cwd_raw.strip():
        return config.repo_root

    target = Path(cwd_raw.strip())
    if not target.is_absolute():
        target = config.repo_root / target

    resolved = target.resolve()
    from chatcopilot.contracts.execution_scope import current_execution_scope

    scope = current_execution_scope()
    if scope is not None:
        if not scope.permits(resolved):
            raise DevPathAccessError("cwd is outside the bound execution resources")
        return resolved
    try:
        resolved.relative_to(config.repo_root)
    except ValueError:
        raise DevPathAccessError(
            f"cwd must be within project root: {cwd_raw}"
        )
    return resolved


def _handle_run_command(args: Mapping[str, Any], _ctx: ToolContext) -> ToolResult:
    config = get_dev_config(require_scope=True)
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolResult(
            ok=False,
            error="command is required",
            error_code="command_required",
            stage="validation",
        )

    task_scope = current_development_task_scope()
    try:
        command_args = command_argv(
            command, validation=task_scope is not None and task_scope.shell_profile == "validation",
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc), error_code="command_blocked", stage="validation")

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

    timeout = config.shell.timeout_default
    if args.get("timeout_seconds") is not None:
        try:
            timeout = max(1, min(int(args["timeout_seconds"]), config.shell.timeout_max))
        except (ValueError, TypeError, OverflowError):
            return ToolResult(ok=False, error="timeout_seconds must be an integer",
                              error_code="command_timeout_invalid", stage="validation")

    try:
        if _ctx.execution_scope is None:
            raise RuntimeError("execution resources are not bound to this command")
        argv = sandbox_command(command_args, scope=_ctx.execution_scope, cwd=cwd)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            timed_out = False
            try:
                result = subprocess.run(
                    argv, shell=False, stdout=stdout, stderr=stderr,
                    timeout=timeout, cwd=str(cwd),
                )
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = None
            artifact_roots = _ctx.execution_scope.project_roots or _ctx.execution_scope.writable_roots
            artifact_root = artifact_roots[0] / "command-output" / uuid4().hex
            outputs: list[str] = []
            previews: list[str] = []
            sizes: dict[str, int] = {}
            for name, stream in (("stdout", stdout), ("stderr", stderr)):
                sizes[name] = stream.tell()
                stream.seek(0)
                destination = artifact_root / (name + ".log")
                write_stream(destination, stream)
                outputs.append(str(destination))
                stream.seek(0)
                preview = stream.read(15_000).decode("utf-8", errors="replace")
                if preview:
                    previews.append(f"[{name}]\n{preview}")
            preview_limited = any(size > 15_000 for size in sizes.values())
            combined = "\n".join(previews)
            if preview_limited:
                combined += "\n[Preview only; complete stdout/stderr are saved in outputs.]"
    except (OSError, ValueError, RuntimeError, IndexError) as exc:
        return ToolResult(ok=False, error=f"Command execution failed: {exc}",
                          error_code="command_execution_failed", stage="execution")

    status = f"timeout after {timeout}s" if timed_out else ("OK" if exit_code == 0 else f"exit code {exit_code}")
    summary = f"[{status}] {command}"
    ok = exit_code == 0 and not timed_out
    return ToolResult(
        ok=ok, summary=summary if ok else "", outputs=outputs,
        data={"command": command, "exit_code": exit_code, "output": combined},
        details={"output_bytes": sizes, "preview_limited": preview_limited},
        error=None if ok else summary,
        error_code="" if ok else ("command_timeout" if timed_out else "command_nonzero_exit"),
        stage="" if ok else "execution",
    )


TOOLS: list[ToolDef] = [
    ToolDef(
        name="run_command",
        summary=(
            "Execute a shell command in the project directory. "
            "Use for running tests, builds, linters, or other dev tasks. "
            "Commands use the host-granted resources. Full stdout/stderr are saved in outputs."
        ),
        input_schema=object_schema(
            {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to project root (default: project root)",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout in seconds, capped by the current instance command budget",
                },
            },
            required=("command",),
        ),
        output_schema=object_schema(
            {
                "command": {"type": "string"},
                "exit_code": {"type": ["integer", "null"], "description": "Observed exit code; null when the command timed out."},
                "output": {"type": "string"},
            },
            required=("command", "exit_code", "output"),
        ),
        handler=_handle_run_command,
        category="dev.shell",
        owner="dev",
        module=__name__,
        access="owner",
        weight="heavy",
    ),
]
