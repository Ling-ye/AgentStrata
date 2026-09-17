"""Pure contracts for Harness worker control; dispatch is not worker liveness."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol


EVALUATION_TERMINAL_STATES = frozenset({"completed", "partial", "cancelled", "interrupted", "error", "not_found"})


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
