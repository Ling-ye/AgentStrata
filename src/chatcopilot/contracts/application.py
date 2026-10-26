"""Independent application and Agent configuration document contracts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentProfile:
    """Agent declaration; the existing runtime parser owns typed field validation."""

    source_path: Path
    raw: dict[str, Any] = field(repr=False)

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent


@dataclass(frozen=True)
class ApplicationSpec:
    """Application declaration and its explicitly referenced Agent profile."""

    source_path: Path
    agent_profile: AgentProfile
    raw: dict[str, Any] = field(repr=False)

    def resolved_configuration(self) -> dict[str, Any]:
        return deepcopy({**self.raw, **self.agent_profile.raw})
