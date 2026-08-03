"""Provider-neutral contracts for Owner asynchronous code tasks."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from chatcopilot.contracts.tools import ToolHandlerError

CODE_TASK_TOOL = "start_code_task"
_CODE_TASK_TITLE_MAX_LENGTH = 72
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MACHINE_PATH_RE = re.compile(
    r"(?:^|\s)(?:~[/\\]|/[^ \t]+|[A-Za-z]:[/\\])"
)
_OBVIOUS_SECRET_RE = re.compile(
    r"(?:"
    r"\b(?:token|password|secret|authorization)\s*[:=]\s*\S+"
    r"|\bbearer\s+\S+"
    r"|\bsk-[A-Za-z0-9_-]{8,}"
    r"|\bghp_[A-Za-z0-9]{8,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{8,}"
    r")",
    flags=re.IGNORECASE,
)

CODE_TASK_ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "preparing",
        "running",
        "validating",
        "delivering",
        "cancel_requested",
    }
)
CODE_TASK_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted"}
)
CODE_TASK_RESUMABLE_STATUSES = frozenset(
    {"failed", "cancelled", "interrupted"}
)
CODE_TASK_TRANSITIONS = {
    "queued": frozenset(
        {"preparing", "cancel_requested", "cancelled", "failed", "interrupted"}
    ),
    "preparing": frozenset(
        {
            "running",
            "delivering",
            "cancel_requested",
            "cancelled",
            "failed",
            "interrupted",
        }
    ),
    "running": frozenset(
        {"validating", "cancel_requested", "cancelled", "failed", "interrupted"}
    ),
    "validating": frozenset(
        {
            "delivering",
            "succeeded",
            "cancel_requested",
            "cancelled",
            "failed",
            "interrupted",
        }
    ),
    "delivering": frozenset(
        {"succeeded", "failed", "interrupted"}
    ),
    "cancel_requested": frozenset({"cancelled", "failed", "interrupted"}),
    "failed": frozenset({"queued"}),
    "cancelled": frozenset({"queued"}),
    "interrupted": frozenset({"queued"}),
    "succeeded": frozenset(),
}


def validate_code_task_title(value: str) -> str:
    """Validate the intentionally public Git commit and pull-request title."""
    title = str(value or "").strip()
    if not title:
        raise ToolHandlerError(
            "public-safe Chinese title is required",
            error_code="code_task_title_missing",
            stage="preparing",
        )
    if (
        len(title) > _CODE_TASK_TITLE_MAX_LENGTH
        or "\n" in title
        or "\r" in title
        or _CONTROL_RE.search(title)
    ):
        raise ToolHandlerError(
            "title must be one line and at most 72 characters",
            error_code="code_task_title_invalid",
            stage="preparing",
        )
    if not _CJK_RE.search(title):
        raise ToolHandlerError(
            "title must contain Chinese text",
            error_code="code_task_title_invalid",
            stage="preparing",
        )
    if (
        "://" in title
        or _MACHINE_PATH_RE.search(title)
        or _OBVIOUS_SECRET_RE.search(title)
    ):
        raise ToolHandlerError(
            "title must not contain URLs, machine paths, or obvious secrets",
            error_code="code_task_title_not_public_safe",
            stage="preparing",
        )
    return title


def validate_code_task_transition(previous: str, current: str) -> None:
    if not previous or previous == current:
        return
    allowed = CODE_TASK_TRANSITIONS.get(previous)
    if allowed is None or current not in allowed:
        raise ValueError(f"invalid code task transition: {previous} -> {current}")


@dataclass(frozen=True)
class CodeTaskLimits:
    timeout_seconds: int = 2 * 60 * 60
    memory_max_bytes: int = 3 * 1024**3
    cpu_quota_percent: int = 400
    tasks_max: int = 256
    active_disk_max_bytes: int = 5 * 1024**3
    heartbeat_seconds: int = 30
    progress_notify_seconds: int = 5 * 60
    cancel_grace_seconds: int = 10


@dataclass(frozen=True)
class CodeTaskAttempt:
    number: int
    prompt: str
    title: str
    submitted_at: float
    status: str = "queued"
    native_session_id: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    error_code: str = ""
    error: str = ""

    def to_payload(self, *, include_prompt: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "number": self.number,
            "title": self.title,
            "submitted_at": self.submitted_at,
            "status": self.status,
            "native_session_id": self.native_session_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
            "error": self.error,
        }
        if include_prompt:
            payload["prompt"] = self.prompt
        return payload


@dataclass(frozen=True)
class CodeTaskStatus:
    task_id: str
    status: str
    stage: str
    message: str
    attempt: int
    updated_at: float
    heartbeat_at: float | None = None
    queue_position: int | None = None
    elapsed_seconds: float | None = None
    resource: Mapping[str, Any] = field(default_factory=dict)
    changed_files: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    error_code: str = ""
    resumable: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "attempt": self.attempt,
            "updated_at": self.updated_at,
            "heartbeat_at": self.heartbeat_at,
            "queue_position": self.queue_position,
            "elapsed_seconds": self.elapsed_seconds,
            "resource": dict(self.resource),
            "changed_files": list(self.changed_files),
            "checks": list(self.checks),
            "error_code": self.error_code,
            "resumable": self.resumable,
        }


__all__ = [
    "CODE_TASK_ACTIVE_STATUSES",
    "CODE_TASK_RESUMABLE_STATUSES",
    "CODE_TASK_TERMINAL_STATUSES",
    "CODE_TASK_TOOL",
    "CODE_TASK_TRANSITIONS",
    "CodeTaskAttempt",
    "CodeTaskLimits",
    "CodeTaskStatus",
    "validate_code_task_transition",
    "validate_code_task_title",
]
