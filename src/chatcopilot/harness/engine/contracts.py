"""The engine's narrow view of project-specific execution and evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from chatcopilot.harness.models import CandidateRef, VerificationPlan


@dataclass(frozen=True)
class VerificationBinding:
    """Trusted adapter projection; the engine never parses a provider's source layout."""

    test_digest: str | None = None
    snapshot_id: str | None = None
    conditions: Any = None
    original_conditions: Any = None
    extensions: dict[str, Any] = field(default_factory=dict)


class CandidateWorkspace(Protocol):
    directory: Path
    worktree: Path
    branch: str

    def snapshot(self, name: str) -> CandidateRef: ...
    def capture(self, number: int, baseline: dict) -> dict: ...
    def digest(self, root: Path) -> str: ...
    def manifest(self, root: Path) -> dict: ...
    def require_git_identity(self) -> None: ...
    def attempt_directory(self, number: int) -> Path: ...
    def copy_draft(self, source: Path, target: Path) -> None: ...
    def patch(self, attempt: dict) -> str: ...
    def regression(self, source: dict) -> dict: ...


class RepairRoles(Protocol):
    def context(
        self, root: Path, baseline: Path, number: int, previous: dict | None, capabilities: dict
    ) -> dict: ...
    def main(
        self, root: Path, number: int, context: dict, options: Callable, cancel: Callable
    ) -> dict: ...
    def plan(
        self, root: Path, number: int, context: dict, options: Callable, cancel: Callable
    ) -> dict: ...
    def bind_plan(self, baseline: Path, number: int, context: dict) -> dict: ...
    def call(
        self,
        role,
        root: Path,
        number: int,
        goal: str,
        evidence: dict,
        options: Callable,
        cancel: Callable,
    ) -> dict: ...
    def submission(self, number: int, context: dict) -> tuple: ...
    def review(
        self, root: Path, evidence: dict, options, output: Path, cancel: Callable
    ) -> dict: ...


class ScenarioPolicy(Protocol):
    purpose: str

    def requirements(self, source: dict) -> dict: ...
    def reproduction(self, source: dict) -> VerificationPlan | None: ...
    def definition_source(self, task: dict, proposal: dict) -> tuple[dict, list[str], dict]: ...
    def retain_conditions(self, source: dict, prior: dict) -> dict: ...
    def binding(self, task: dict) -> VerificationBinding: ...
    def external_id(self, source: dict, run_id: str) -> str | None: ...
    def review_extension(
        self,
        task: dict,
        decision: dict,
        review: dict,
        baseline: Path,
        candidate: Path,
        paths: list[str],
    ) -> dict: ...
    def failure_brief(self, **kwargs) -> dict: ...
    def classify_check(self, row: dict) -> str: ...
