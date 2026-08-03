"""Tool pack DTO contracts shared by BotSpec validation and Agent discovery."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolPackEntry:
    name: str
    manifest_module: str | None = None
    manifest_builder: str = "build_manifest"
    tool_modules: tuple[str, ...] = ()
    http_route_modules: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class ToolFeatureEntry:
    name: str
    description: str = ""


@dataclass(frozen=True)
class ToolPackPrompt:
    """Runtime prompt metadata for a tool pack."""

    name: str
    prompt_fragments: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "ToolFeatureEntry",
    "ToolPackEntry",
    "ToolPackPrompt",
]
