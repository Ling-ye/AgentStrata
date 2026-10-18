"""Host-owned trace and result handling around a coding-agent runner."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from chatcopilot.core.private_sqlite import private_directory, storage_error_details
from chatcopilot.core.file_integrity import require_regular_file
from chatcopilot.core.trace_archive import TraceArchive
from chatcopilot.core.trace_capture import TraceCapture, capture_scope
from chatcopilot.harness.agent_types import AgentCall, AgentResult, Role, role_result
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.flow_records import step_binding
from chatcopilot.harness.models import HarnessError


def _secure_drafts(call: AgentCall, output: Path) -> None:
    """The host seals generated files before adopting them as private evidence."""
    names = (("test_reproduction.py", "agent_case.json") if call.role == Role.TEST else
             ("evidence.md",) if call.role == Role.CODING and
             call.evidence.get("source", {}).get("skill_learning") else ())
    if not names:
        return
    name = "draft"
    try:
        draft = private_directory(output / "draft")
        for name in names:
            path = draft / name
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            try:
                opened = os.fstat(descriptor)
                require_regular_file(opened, owner_uid=os.getuid(), single_link=True)
                os.fchmod(descriptor, 0o600)
                current = path.lstat()
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError("draft identity changed during adoption")
            finally:
                os.close(descriptor)
    except (OSError, ValueError) as exc:
        if storage_error_details(exc):
            raise
        raise HarnessError("test_definition", f"草案 {name} 无法收录：{safe_error(exc)}") from exc


def execute_agent_call(
    worktree: Path,
    call: AgentCall,
    output: Path,
    run: Callable[[], dict[str, Any]],
    *,
    trace_publisher: Callable[[Path, dict[str, Any]], None] | None = None,
) -> AgentResult:
    """Record one role execution; the provider supplies only its native result."""
    capture = TraceCapture(
        {"kind": "harness", "execution_id": uuid.uuid4().hex, **step_binding(),
         "phase": call.role.value, "role": call.role.value},
        roots={"workspace": worktree, "output": output},
    )
    execution: dict[str, Any] = {}
    status = "failed"
    try:
        with capture_scope(capture):
            try:
                execution = run()
            except HarnessError:
                raise
            except (ValueError, TypeError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                if isinstance(exc, OSError) and storage_error_details(exc):
                    raise
                message = f"原生会话启动或执行失败：{type(exc).__name__}: {safe_error(exc)}"
                capture.record(
                    {"kind": "coding_error", "status": "failed", "data": {"error_code": "coding_environment"}},
                    {"error": message},
                )
                raise HarnessError("coding_environment", message) from exc
            try:
                value = json.loads(execution.pop("final_text"))
            except (ValueError, TypeError) as exc:
                raise HarnessError("invalid_role_result", "角色输出不是有效结构化产物") from exc
            payload = role_result(
                call.role, value,
                governance=call.evidence.get("source", {}).get("kind") == "code_health",
            )
            _secure_drafts(call, output)
        status = "completed"
        return AgentResult(payload, execution)
    finally:
        try:
            execution["trace"] = TraceArchive(output / "traces").save(capture, status, retained=True)
            if trace_publisher:
                trace_publisher(output / "traces", execution["trace"])
        except Exception:
            logging.getLogger(__name__).warning("Harness role trace archive failed")
