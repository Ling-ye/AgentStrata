"""Tool pack DTO contracts shared by BotSpec validation and Agent discovery."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolModuleBinding:
    """Exact tool names contributed by one repository module to a tool pack."""

    module: str
    tool_names: tuple[str, ...]


@dataclass(frozen=True)
class ToolPackEntry:
    name: str
    manifest_module: str | None = None
    manifest_builder: str = "build_manifest"
    tool_bindings: tuple[ToolModuleBinding, ...] = ()
    http_route_modules: tuple[str, ...] = ()
    description: str = ""

    @property
    def tool_modules(self) -> tuple[str, ...]:
        """Return declared module paths without exposing mutable registry state."""

        return tuple(binding.module for binding in self.tool_bindings)

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Return the ordered union of exact tool names exposed by this pack."""

        return tuple(
            dict.fromkeys(
                tool_name
                for binding in self.tool_bindings
                for tool_name in binding.tool_names
            )
        )


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
    "ToolModuleBinding",
    "ToolPackEntry",
    "ToolPackPrompt",
]
