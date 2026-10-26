"""Explicit user goals and write scope for integration and improvement tasks."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re

from chatcopilot.contracts.adapter_approval import AdapterApprovalEnvelope, validate_adapter_approval


@dataclass(frozen=True)
class ImprovementRequest:
    task_type: str
    bot_id: str
    goal: str
    allowed_paths: tuple[str, ...]
    goal_case_ids: tuple[str, ...] = ()
    suite_id: str = "agentstrata-application-bridges-v1"
    source_envelope: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.task_type not in {"integrate", "improve"}:
            raise ValueError("task_type must be integrate or improve")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.bot_id):
            raise ValueError("A registered application id is required")
        if not isinstance(self.goal, str) or not self.goal.strip() or not self.allowed_paths:
            raise ValueError("A concrete goal and explicit writable paths are required")
        for name in self.allowed_paths:
            path = PurePosixPath(name)
            if not isinstance(name, str) or path.is_absolute() or str(path) != name or ".." in path.parts or name == ".":
                raise ValueError("Writable paths must be canonical repository-relative paths")
        if len(set(self.allowed_paths)) != len(self.allowed_paths) or len(set(self.goal_case_ids)) != len(self.goal_case_ids):
            raise ValueError("Duplicate scope or Case ids")
        if self.source_envelope:
            envelope = AdapterApprovalEnvelope(**self.source_envelope)
            errors = validate_adapter_approval(envelope)
            if errors:
                raise ValueError("; ".join(errors))

