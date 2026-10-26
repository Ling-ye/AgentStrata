"""Accepted source inputs and installed service facts for local version switching."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chatcopilot.plugins.types import ServiceDefinition


@dataclass(frozen=True)
class ReleaseSource:
    task_id: str
    bot_id: str
    baseline: Path
    baseline_manifest: dict[str, Any]
    candidate: Path
    candidate_manifest: dict[str, Any]
    receipt: dict[str, Any]
    bot_relative: str
    runtime_environment: dict[str, Any]
    model_identity: str


@dataclass(frozen=True)
class ActivationTarget:
    id: str
    root: Path
    bot_spec: Path
    env_file: Path
    state_root: Path
    services: tuple[ServiceDefinition, ...]
    component_paths: dict[str, str]
    units: dict[str, str]

    @property
    def control(self):
        return self.root / ".agentstrata-versions"
