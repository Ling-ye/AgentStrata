"""Pure installed-service and accepted-source data shared by control domains."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    command: tuple[str, ...]
    cwd: str
    environment_file: str
    port: int
    ready_url: str = ""
    versioned: bool = False


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
