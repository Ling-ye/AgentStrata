"""Shared repository mutation service for every main-agent backend."""
from __future__ import annotations

from chatcopilot.external_tools.repository_tasks.runtime import (
    ChangeState,
    abort_change,
    apply_change_patch,
    change_diff,
    load_state,
    prepare_change,
    publish_change_overlay,
    record_review,
    run_checks,
)


class RepositoryTaskService:
    """Prepare, validate, publish, and abort exact task-scoped overlays."""

    def prepare(
        self, repository: str, objective: str, *, task_id: str = ""
    ) -> ChangeState:
        return prepare_change(repository, objective, change_id=task_id)

    def apply_patch(self, task_id: str, patch: str) -> ChangeState:
        return apply_change_patch(task_id, patch)

    def diff(self, task_id: str) -> str:
        return change_diff(task_id)

    def review(self, task_id: str, *, ok: bool, summary: str) -> ChangeState:
        return record_review(task_id, ok=ok, summary=summary)

    def check(
        self, task_id: str, check_ids: tuple[str, ...] = ()
    ) -> tuple[ChangeState, list[dict]]:
        return run_checks(task_id, check_ids)

    def publish(self, task_id: str) -> ChangeState:
        return publish_change_overlay(task_id)

    def abort(self, task_id: str) -> ChangeState:
        return abort_change(task_id)

    def status(self, task_id: str) -> ChangeState:
        return load_state(task_id)


repository_tasks = RepositoryTaskService()

__all__ = ["RepositoryTaskService", "repository_tasks"]

