"""Host-owned trace and result handling around a coding-agent runner."""
from __future__ import annotations

import json
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from chatcopilot.core.private_sqlite import storage_error_details
from chatcopilot.core.trace_archive import TraceArchive
from chatcopilot.core.trace_capture import TraceCapture, capture_scope
from chatcopilot.harness.agent_types import AgentCall, AgentResult, role_result
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.flow_records import step_binding
from chatcopilot.harness.models import HarnessError


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
        status = "completed"
        return AgentResult(payload, execution)
    finally:
        try:
            execution["trace"] = TraceArchive(output / "traces").save(capture, status, retained=True)
            if trace_publisher:
                trace_publisher(output / "traces", execution["trace"])
        except Exception:
            logging.getLogger(__name__).warning("Harness role trace archive failed")
