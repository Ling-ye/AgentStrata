"""Source port: freeze provenance independently from any execution backend."""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.models import HarnessError, ProblemEvidence


class RepairSources:
    def __init__(self, evaluation: Any, task_reader: Callable[[str, str], dict[str, Any]] | None) -> None:
        self.evaluation = evaluation
        self.task_reader = task_reader

    def load(self, reference: dict[str, str]) -> ProblemEvidence:
        if reference.get("kind") == "robot_task":
            if self.task_reader is None:
                raise HarnessError("source_unavailable", "尚未装配机器人任务观测入口")
            source = self.task_reader(reference["bot_id"], reference["run_id"])
            ident = reference["run_id"]
        elif reference.get("case_instance_id"):
            ident = reference["case_instance_id"]
            source = self.evaluation.source_instance(ident)
        else:
            ident = reference["case_ref"]
            source = self.evaluation.source(reference["evaluation_id"], ident, reference["target_id"])
        raw = json_text(source)
        return ProblemEvidence(ident, source.get("kind", "evaluation"), hashlib.sha256(raw.encode()).hexdigest(), source)
