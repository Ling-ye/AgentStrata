"""Tool pack DTO contracts shared by BotSpec validation and Agent discovery."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolModuleBinding:
    """Exact tool names contributed by one repository module to a tool pack."""

    module: str
    tool_names: tuple[str, ...]


@dataclass(frozen=True)
class ToolPackEntry:
    name: str
    policy_module: str | None = None
    policy_builder: str = "build_policy"
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
class ToolPackPolicy:
    """Stable cross-tool policy contributed by the component catalog."""

    id: str
    content: str
    applies_to_roles: tuple[str, ...] = ("owner", "admin", "user")
    applies_to_channels: tuple[str, ...] = ("private", "group")

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.content.strip():
            raise ValueError("tool pack policy id and content are required")
        if not set(self.applies_to_roles) <= {"owner", "admin", "user"}:
            raise ValueError("tool pack policy contains an unknown role")
        if not set(self.applies_to_channels) <= {"private", "group"}:
            raise ValueError("tool pack policy contains an unknown channel")


def tool_pack_policies(
    pack_id: str,
    *contents: str,
    applies_to_roles: tuple[str, ...] = ("owner", "admin", "user"),
    applies_to_channels: tuple[str, ...] = ("private", "group"),
) -> tuple[ToolPackPolicy, ...]:
    """Build stable ordered policy ids for a catalog-owned tool pack."""

    return tuple(
        ToolPackPolicy(
            id=f"{pack_id}.{index}",
            content=content,
            applies_to_roles=applies_to_roles,
            applies_to_channels=applies_to_channels,
        )
        for index, content in enumerate(contents, start=1)
    )


__all__ = [
    "ToolFeatureEntry",
    "ToolModuleBinding",
    "ToolPackEntry",
    "ToolPackPolicy",
    "tool_pack_policies",
]
