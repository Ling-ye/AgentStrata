"""Tool-provider and tool-pack contracts shared by discovery and BotSpec."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from chatcopilot.contracts.tools import ToolDef


ToolPackRuntimeScope = Literal["static", "runtime", "agent_session", "host_session"]
ToolPackProjectionProfile = Literal["interactive", "detached"]
CAPABILITY_PROVIDER_FACTORY = "build_provider"
TOOL_PACK_PROJECTION_PROFILES: tuple[ToolPackProjectionProfile, ...] = (
    "interactive",
    "detached",
)


@dataclass(frozen=True)
class ToolProvider:
    """One explicitly registered source of tools grouped by BotSpec pack id."""

    id: str
    packs: Mapping[str, tuple[ToolDef, ...]]
    module: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        normalized = {
            pack_id: tuple(tools)
            for pack_id, tools in self.packs.items()
        }
        object.__setattr__(self, "packs", MappingProxyType(normalized))

    @property
    def pack_names(self) -> tuple[str, ...]:
        return tuple(self.packs)


@dataclass(frozen=True)
class ToolPackEntry:
    """Central index entry containing no duplicated tool-name membership."""

    name: str
    provider_module: str | None = None
    dynamic: bool = False
    policy_module: str | None = None
    policy_builder: str = "build_policy"
    http_route_modules: tuple[str, ...] = ()
    description: str = ""
    runtime_scope: ToolPackRuntimeScope = "static"
    projection_profiles: tuple[ToolPackProjectionProfile, ...] = (
        "interactive",
        "detached",
    )
    provider_factory_module: str | None = None
    factory_order: int = 0
    session_default_enabled: bool = False


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


def static_tool_provider(
    provider_id: str,
    *,
    packs: Mapping[str, tuple[ToolDef, ...]],
    module: str,
    description: str = "",
) -> ToolProvider:
    """Construct an explicit provider for domain-owned static tool tuples."""

    return ToolProvider(
        id=provider_id,
        packs=packs,
        module=module,
        description=description,
    )


__all__ = [
    "CAPABILITY_PROVIDER_FACTORY",
    "TOOL_PACK_PROJECTION_PROFILES",
    "ToolFeatureEntry",
    "ToolPackEntry",
    "ToolPackPolicy",
    "ToolPackProjectionProfile",
    "ToolPackRuntimeScope",
    "ToolProvider",
    "static_tool_provider",
    "tool_pack_policies",
]
