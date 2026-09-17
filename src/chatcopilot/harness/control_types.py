"""Pure contracts for Harness worker control; dispatch is not worker liveness."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol


EVALUATION_TERMINAL_STATES = frozenset({"completed", "partial", "cancelled", "interrupted", "error", "not_found"})


def external_evaluation_id(source: dict[str, Any], run_id: str | None) -> str | None:
    """Resolve a verification run to its external Agent execution, if any."""
    if not run_id:
        return None
    if source.get("agent_source"):
        return run_id + "-agent"
    return None if source.get("test_sha256") else run_id


class WorkerState(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DispatchResult:
    state: Literal["scheduled", "failed", "unknown"]
    code: str = ""
    message: str = ""


class WorkerControlPort(Protocol):
    def observe(self, task: dict[str, Any]) -> WorkerState: ...
    def launch(self, task: dict[str, Any]) -> DispatchResult: ...
    def launch_delivery(self, task: dict[str, Any]) -> DispatchResult: ...


class EvaluationControlPort(Protocol):
    def cancel(self, evaluation_id: str) -> None: ...
    def execution_status(self, evaluation_id: str) -> str: ...
