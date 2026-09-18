"""Small, persisted business steps; execution bodies remain in their existing archives."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
from typing import Any, Iterator
import uuid

from chatcopilot.core.observability_redaction import redact_observability_payload
from chatcopilot.harness.models import Cancelled
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.store import HarnessStore

_STEP: ContextVar[dict[str, str]] = ContextVar("harness_flow_step", default={})


def step_binding() -> dict[str, str]:
    return dict(_STEP.get())


@dataclass
class StepResult:
    conclusion: str = "执行完成，业务结论见结果与依据"
    outcome: str = "completed"
    evidence: dict[str, Any] = field(default_factory=dict)


@contextmanager
def record_step(store: HarnessStore | None, task_id: str, phase: str, title: str, *,
                group: str, inputs: dict[str, Any], locator: dict[str, Any] | None = None,
                attempt: int | None = None, revision: int | None = None,
                source_id: str | None = None, generation: int | None = None) -> Iterator[StepResult]:
    result = StepResult()
    task = store.get(task_id) if store is not None else {}
    enabled = store is not None
    if not enabled:
        yield result
        return
    ident = "step-" + uuid.uuid4().hex
    safe = redact_observability_payload(inputs)
    row = {"id": ident, "phase": phase, "title": title, "parent_id": group,
           "attempt": attempt, "revision": revision, "generation": generation if generation is not None else task.get("plan_generation"),
           "started_at": time.time(), "finished_at": None, "status": "running",
           "input": safe.value, "input_truncated": safe.truncated,
           "conclusion": "执行中，尚无结论", "locator": locator or {}, "source_id": source_id}
    store.save_flow_step(task_id, row)
    token = _STEP.set({"task_id": task_id, "flow_step_id": ident})
    try:
        yield result
    except BaseException as exc:
        result.outcome = "cancelled" if isinstance(exc, Cancelled) else "failed"
        result.conclusion = safe_error(exc)
        result.evidence = {"error_code": getattr(exc, "code", type(exc).__name__)}
        raise
    finally:
        _STEP.reset(token)
        store.save_flow_step(task_id, {**row, "finished_at": time.time(), "status": result.outcome,
            "conclusion": redact_observability_payload(result.conclusion).value,
            "evidence": redact_observability_payload(result.evidence).value})


def source_inputs(source: dict[str, Any]) -> dict[str, Any]:
    return {key: source[key] for key in ("kind", "run_id", "case_instance_id", "evaluation_id",
        "case_id", "revision", "feedback", "warnings") if key in source}
