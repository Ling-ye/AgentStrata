"""Role contracts. A model result is a proposal, never an execution receipt."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from chatcopilot.harness.models import HarnessError, RepairOptions, CodingOptions
from chatcopilot.harness.repair_types import SUBMISSION_SCHEMA, submission


class Role(str, Enum):
    MAIN = "main"
    PLAN = "plan"
    CODING = "coding"
    TEST = "test"
    REVIEW = "review"


def object_schema(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
TEXTS = {"type": "array", "items": TEXT}
SCHEMAS = {
    Role.MAIN: object_schema(next_role={"type": "string", "enum": ["plan", "coding", "test", "blocked"]},
                             summary=TEXT, unresolved=TEXTS),
    Role.PLAN: object_schema(decision={"type": "string", "enum": ["proceed", "blocked"]}, summary=TEXT, evidence_refs=TEXTS, changes=TEXTS,
                            verification_order={"type": "string", "enum": ["test_first", "code_first", "existing"]},
                            goal_capabilities=SUBMISSION_SCHEMA["properties"]["goal_capabilities"], unresolved=TEXTS),
    Role.CODING: object_schema(summary=TEXT, notes=TEXTS, needs_replan={"type": "boolean"},
                              gaps=SUBMISSION_SCHEMA["properties"]["gaps"]),
    Role.TEST: SUBMISSION_SCHEMA,
    Role.REVIEW: object_schema(decision={"type": "string", "enum": ["approved", "rejected", "inconclusive"]},
                              problem=TEXT, reason=TEXT, evidence_refs={"type": "array", "minItems": 1, "items": {"type": "string", "enum": ["source", "reproduction", "verification", "patch", "regression"]}}),
}


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str
    kind: str
    revision: int


@dataclass(frozen=True)
class AgentCall:
    task_id: str
    role: Role
    revision: int
    goal: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class AgentResult:
    payload: dict[str, Any]
    execution: dict[str, Any]


class AgentRunner(Protocol):
    def execute(self, worktree: Path, call: AgentCall, options: RepairOptions | CodingOptions,
                output: Path, cancel: Callable[[], None]) -> AgentResult: ...


def role_result(role: Role, value: Any) -> dict[str, Any]:
    from jsonschema import ValidationError, validate
    try:
        validate(value, SCHEMAS[role])
    except ValidationError as exc:
        raise HarnessError("test_definition" if role == Role.TEST else "invalid_role_result",
                           f"{role.value} 未返回完整结构化产物") from exc
    if role == Role.TEST:
        return submission(value)
    summary = value.get("summary", value.get("reason", ""))
    if not summary.strip():
        raise HarnessError("invalid_role_result", "角色产物缺少结论")
    if role == Role.MAIN and value["next_role"] == "blocked" and not value["unresolved"]:
        raise HarnessError("invalid_role_result", "阻塞决定必须说明缺口")
    if role == Role.PLAN and value["decision"] == "blocked" and not value["unresolved"]:
        raise HarnessError("invalid_role_result", "计划阻塞必须说明缺少的必要条件")
    return value


@dataclass(frozen=True)
class AcceptedCandidate:
    candidate_digest: str
    goal_digest: str
    verification_digest: str
    review_binding: str
    attempt: int


def retry_role(stage: str, code: str) -> Role:
    if code in {"needs_replan", "invalid_role_result"} or stage in {"main", "plan", "review"}:
        return Role.MAIN
    if stage == "definition" or code in {"test_definition", "verification_test_definition", "acceptance_gap"}:
        return Role.TEST
    return Role.CODING
