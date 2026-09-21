"""Provider-neutral background job execution context."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from chatcopilot.contracts.model_selection import WorkerModelSelection

StageUpdater = Callable[[str, str, str, Mapping[str, object] | None], None]


@dataclass(frozen=True)
class JobExecutionContext:
    job_id: str
    job_dir: Path
    update_status: StageUpdater
    worker_model_selection: WorkerModelSelection | None = None

    @property
    def worktree(self) -> Path:
        return self.job_dir / "worktree"

    def update_stage(
        self,
        stage: str,
        message: str,
        *,
        error_code: str = "",
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.update_status(stage, message, error_code, details)


__all__ = ["JobExecutionContext", "StageUpdater"]
