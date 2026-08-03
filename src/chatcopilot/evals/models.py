"""Structured contracts for AgentStrata evaluations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SuiteKind = Literal["product", "knowledge", "reasoning", "code", "agent", "tool", "web", "context", "safety"]
RunStatus = Literal["passed", "failed", "skipped", "error", "unavailable"]


@dataclass(frozen=True)
class BenchmarkStandard:
    """Metadata for a benchmark that can be manually enabled."""

    suite_id: str
    name: str
    kind: SuiteKind
    value: str
    recommendation: str
    cadence: str
    requires_bot: bool = True
    requires_external_data: bool = False
    setup_hint: str = ""
    official_url: str = ""


@dataclass(frozen=True)
class EvalCase:
    """One evaluation task."""

    case_id: str
    input: str
    category: str
    expected_behavior: str
    must_have: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()
    context: str = ""
    rubric: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgeResult:
    """Structured scoring output for a case."""

    score: float
    max_score: float
    passed: bool
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalCaseResult:
    """Execution and judgment result for one case."""

    case_id: str
    suite_id: str
    status: RunStatus
    score: float = 0.0
    max_score: float = 1.0
    final_text: str = ""
    stop_reason: str = ""
    duration_seconds: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    events: tuple[dict[str, Any], ...] = ()
    judge: JudgeResult | None = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalRunResult:
    """Aggregated result for a suite run."""

    suite_id: str
    bot: str | None
    status: RunStatus
    started_at: str
    duration_seconds: float
    cases: tuple[EvalCaseResult, ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses recursively into JSON-serializable values."""

    if hasattr(value, "__dataclass_fields__"):
        return {key: to_jsonable(raw) for key, raw in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(raw) for key, raw in value.items()}
    return value
