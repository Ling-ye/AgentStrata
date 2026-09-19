"""Role contracts. A model result is a proposal, never an execution receipt."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
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

# The same roles use a distinct output contract for repository maintenance.
FINDING = object_schema(id=TEXT, summary=TEXT, impact=TEXT, principle_refs=TEXTS,
    evidence={"type": "array", "minItems": 1, "items": object_schema(path=TEXT,
        start_line={"type": "integer", "minimum": 1}, end_line={"type": "integer", "minimum": 1})},
    affected_paths=TEXTS, acceptance_criteria=TEXTS,
    disposition={"type": "string", "enum": ["automatic", "needs_decision"]})
GC_PLAN = object_schema(**{
    **SCHEMAS[Role.PLAN]["properties"],
    "decision": {"type": "string", "enum": ["proceed", "blocked", "no_changes", "needs_review"]},
    "findings": {"type": "array", "maxItems": 1, "items": FINDING}, "selected_finding_id": TEXT,
    "inspected_paths": TEXTS, "uninspected": TEXTS,
})
GC_REVIEW = object_schema(**{
    **SCHEMAS[Role.REVIEW]["properties"],
    "finding_id": TEXT, "behavior_preserved": {"type": "boolean"},
    "improvements": {"type": "array", "items": object_schema(path=TEXT, before=TEXT, after=TEXT, reason=TEXT)},
})
GC_CODING = deepcopy(SCHEMAS[Role.CODING])
GC_TEST = deepcopy(SCHEMAS[Role.TEST])
for contract in (GC_CODING, GC_TEST):
    contract["properties"]["gaps"]["items"]["properties"]["code"] = {
        "type": "string", "enum": ["fixture_missing", "material_missing", "permission_missing"],
        "description": "A concrete missing prerequisite; pending host verification belongs in notes."}


def role_schema(role: Role, *, governance: bool = False) -> dict[str, Any]:
    return ({Role.PLAN: GC_PLAN, Role.CODING: GC_CODING, Role.TEST: GC_TEST, Role.REVIEW: GC_REVIEW}.get(role, SCHEMAS[role])
            if governance else SCHEMAS[role])


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


def role_result(role: Role, value: Any, *, governance: bool = False) -> dict[str, Any]:
    from jsonschema import ValidationError, validate
    try:
        validate(value, role_schema(role, governance=governance))
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
