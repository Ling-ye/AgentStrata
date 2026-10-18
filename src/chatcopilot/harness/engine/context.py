"""Invocation-scoped capabilities. No runtime objects are stored in graph state."""

from __future__ import annotations
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
from typing import Any, Callable
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.models import (
    RepairOptions,
    VerificationPlan,
    CandidateRef,
    Cancelled,
    HarnessError,
    acceptance_digest,
    Verifier,
)
from chatcopilot.harness.task_budget import TaskBudget
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.engine.contracts import CandidateWorkspace, RepairRoles, ScenarioPolicy


@dataclass
class HarnessContext:
    store: Any = field(repr=False)
    task_id: str
    verifier: Verifier = field(repr=False)
    scenario: ScenarioPolicy = field(repr=False)
    workspace_factory: Callable[..., CandidateWorkspace] = field(repr=False)
    roles_factory: Callable[..., RepairRoles] = field(repr=False)
    preparer: Callable | None = field(default=None, repr=False)
    _artifacts: CandidateWorkspace | None = field(default=None, init=False, repr=False)
    _roles: RepairRoles | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self.options = RepairOptions(**self.store.get(self.task_id)["options"])
        self.budget = TaskBudget(self.store, self.task_id)
        self.records = ArtifactRepository(self.store.root / "jobs" / self.task_id)

    @property
    def artifacts(self) -> CandidateWorkspace:
        if self._artifacts is None:
            self._artifacts = self.workspace_factory(self.store.get(self.task_id))
        return self._artifacts

    @property
    def roles(self) -> RepairRoles:
        if self._roles is None:
            self._roles = self.roles_factory(self.artifacts)
        return self._roles

    @property
    def governance(self) -> bool:
        return self.scenario.purpose == "governance"

    @property
    def baseline_candidate(self) -> CandidateRef:
        return self.artifacts.snapshot("baseline")

    def require_purpose(self, plan: VerificationPlan) -> None:
        if plan.purpose != self.scenario.purpose:
            raise HarnessError("verification_purpose", "验收用途与宿主冻结来源不一致")

    def cancel(self):
        self.budget.check()

    def remaining(self):
        self.cancel()
        return replace(
            self.options,
            timeout_seconds=max(1, int(self.budget.remaining))
            if self.budget.remaining is not None
            else None,
        )

    def finish(self, status, code="", message=""):
        return self.store.update(
            self.task_id,
            status=status,
            stage="done",
            stop_reason=code or status,
            error_code=code or None,
            message=message,
            next_action="review_candidate" if status == "needs_review" else None,
        )

    def put(self, kind, number, value):
        return asdict(self.records.put(kind, number, value))

    def context(self, state):
        return self.records.read(state["context_ref"])

    def attempt(self, state):
        return next(
            row for row in self.store.attempts(self.task_id) if row["number"] == state["attempt"]
        )

    def save(self, state, attempt):
        self.store.save_attempt(self.task_id, state["attempt"], attempt)

    def error(self, state, name, exc):
        return self.put(
            "graph_error",
            state.get("attempt", 0),
            {
                "phase": name,
                "code": getattr(
                    exc,
                    "code",
                    "test_definition"
                    if isinstance(exc, (SyntaxError, ValueError))
                    else "execution_error",
                ),
                "message": safe_error(exc),
                "cancelled": isinstance(exc, Cancelled),
                "evidence": getattr(exc, "evidence", None),
            },
        )

    def inputs(self, state):
        task = self.store.get(self.task_id)
        attempt = self.attempt(state)
        plan = VerificationPlan.from_payload(task["verification_plan"])
        candidate = CandidateRef(
            Path(attempt["snapshot_path"]), attempt["candidate_digest"], task["base_commit"]
        )
        return task, attempt, plan, candidate

    def recover_accepted(self):
        """Restore a lifecycle projection only from a fully durable host receipt."""
        task = self.store.get(self.task_id)
        if task["status"] == "fixed":
            return
        if task["status"] in {"cancel_requested", "cancelled"}:
            raise Cancelled()
        receipt = task.get("accepted_candidate") or {}
        attempts = [
            row
            for row in self.store.attempts(self.task_id)
            if row["number"] == receipt.get("attempt") and row.get("status") == "accepted"
        ]
        if (
            len(attempts) != 1
            or not task.get("verified_digest")
            or receipt.get("candidate_digest") != task["verified_digest"]
            or receipt.get("goal_digest") != task.get("acceptance", {}).get("sha256")
            or receipt.get("verification_digest") != acceptance_digest(task, attempts[0])
            or receipt.get("review_binding") != attempts[0].get("review", {}).get("binding")
            or manifest_digest(task.get("verified_manifest", {})) != task["verified_digest"]
        ):
            raise HarnessError("incomplete_verification", "检查点缺少可恢复的完整宿主验收凭据")
        self.store.update(
            self.task_id,
            if_status=frozenset({"running"}),
            status="fixed",
            stage="done",
            current_attempt=receipt["attempt"],
            error_code=None,
            stop_reason="fixed",
            message="",
        )
