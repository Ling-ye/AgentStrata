"""Plain checkpoint data; executable dependencies belong to HarnessContext."""

from __future__ import annotations

from typing import Any, TypedDict

GRAPH_VERSION = 2


class GraphIdentity(TypedDict, total=False):
    identity: str
    version: int
    phase: str


class RepairInput(GraphIdentity, total=False):
    task_id: str
    pipeline_version: int
    problem_ref: dict[str, Any] | None
    policy_ref: dict[str, Any] | None
    error_ref: dict[str, Any] | None
    outcome: str
    retry_from: str
    verification_retry: int


class RepairOutput(TypedDict, total=False):
    outcome: str
    accepted_candidate_ref: dict[str, Any] | None
    error_ref: dict[str, Any] | None
    phase: str


class RepairState(RepairInput, RepairOutput, total=False):
    attempt: int
    route: str
    context_ref: dict[str, Any] | None
    submission_ref: dict[str, Any] | None
    deferred_error_ref: dict[str, Any] | None
    pending_roles: list[str]
    replaying: bool
    goal_ref: dict[str, Any] | None
    plan_ref: dict[str, Any] | None
    candidate_ref: dict[str, Any] | None
    verification_ref: dict[str, Any] | None
    confirmation_ref: dict[str, Any] | None
    review_ref: dict[str, Any] | None
    failure_ref: dict[str, Any] | None


class AcceptanceInput(GraphIdentity, total=False):
    attempt: int
    context_ref: dict[str, Any] | None
    submission_ref: dict[str, Any] | None
    replaying: bool
    candidate_ref: dict[str, Any] | None
    retry_from: str


class AcceptanceOutput(RepairOutput, total=False):
    goal_ref: dict[str, Any] | None
    plan_ref: dict[str, Any] | None
    verification_ref: dict[str, Any] | None
    confirmation_ref: dict[str, Any] | None
    review_ref: dict[str, Any] | None


class AcceptanceState(AcceptanceInput, AcceptanceOutput):
    pass


class DeliveryState(GraphIdentity, total=False):
    route: str
    outcome: str
    error_ref: dict[str, Any] | None
    preflighted: bool


class BatchState(GraphIdentity, total=False):
    route: str
    outcome: str
    sequence: int
    child_id: str
    kind: str
    context_ref: dict[str, Any] | None
    preflighted: bool


def acceptance_route(state: AcceptanceState) -> str:
    return "done" if state.get("error_ref") or state.get("outcome") else "next"
