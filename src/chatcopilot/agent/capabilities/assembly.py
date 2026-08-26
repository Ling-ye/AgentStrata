"""Materialize cataloged Agent capabilities through one provider convention."""
from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Callable, Sequence

from chatcopilot.agent.rag.provider import Retriever
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.agent.tools.executor import BackgroundSubmitter, PermissionFilter
from chatcopilot.agent.tools.file_delivery import FileSender
from chatcopilot.agent.tools.workspace_context import WorkspaceService
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.tool_packs import (
    CAPABILITY_PROVIDER_FACTORY,
    ToolPackEntry,
    ToolPackProjectionProfile,
    ToolProvider,
)
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.tool_packs.catalog import (
    get_tool_pack_entry,
    session_tool_pack_entries,
)

ModuleLoader = Callable[[str], ModuleType]


class CapabilityMaterializationError(RuntimeError):
    """A trusted capability provider could not be materialized."""

    def __init__(self, pack_id: str, reason: str) -> None:
        self.pack_id = pack_id
        self.reason = reason
        super().__init__(
            f"capability materialization failed: pack={pack_id}; reason={reason}"
        )


@dataclass(frozen=True)
class RuntimeCapabilityContext:
    """Immutable Bot-runtime values needed by runtime provider factories."""

    skill_index: tuple[SkillIndexEntry, ...] = ()


@dataclass(frozen=True)
class SessionCapabilityContext:
    """Narrow immutable ports available to Agent-owned session contributors."""

    session_id: str
    backend_id: str
    main_llm: LLMClient
    research_llm: LLMClient
    runtime_config: ChatConfig
    subagents: SubagentSpec
    base_tools: tuple[ToolDef, ...]
    subagent_tools: tuple[ToolDef, ...]
    mcp_configs: tuple[McpServerConfig, ...]
    memory_snapshot: str
    retriever: Retriever | None
    search_circuit: SearchCircuitBreaker
    background_submitter: BackgroundSubmitter | None = None
    permission_filter: PermissionFilter | None = None
    file_sender: FileSender | None = None
    workspace_service: WorkspaceService | None = None
    contributed_tools: tuple[ToolDef, ...] = ()


def materialize_runtime_providers(
    tool_pack_names: Sequence[str],
    context: RuntimeCapabilityContext,
    *,
    module_loader: ModuleLoader = importlib.import_module,
) -> tuple[ToolProvider, ...]:
    """Build selected runtime providers in deterministic catalog order."""

    entries = sorted(
        (
            entry
            for name in tool_pack_names
            if (entry := get_tool_pack_entry(name)) is not None
            and entry.runtime_scope == "runtime"
            and entry.provider_factory_module is not None
        ),
        key=lambda entry: (entry.factory_order, entry.name),
    )
    providers: list[ToolProvider] = []
    for entry in entries:
        provider = _materialize_provider(
            entry,
            context,
            allow_none=False,
            module_loader=module_loader,
        )
        if provider is not None:
            providers.append(provider)
    return tuple(providers)


def materialize_session_providers(
    context: SessionCapabilityContext,
    *,
    tool_pack_names: Sequence[str] | None = (),
    profile: ToolPackProjectionProfile = "interactive",
    module_loader: ModuleLoader = importlib.import_module,
) -> tuple[ToolProvider, ...]:
    """Build selected session contributors in deterministic catalog order."""

    providers: list[ToolProvider] = []
    contributed_tools: list[ToolDef] = []
    for entry in session_tool_pack_entries(tool_pack_names, profile=profile):
        provider = _materialize_provider(
            entry,
            replace(context, contributed_tools=tuple(contributed_tools)),
            allow_none=True,
            module_loader=module_loader,
        )
        if provider is None:
            continue
        providers.append(provider)
        contributed_tools.extend(provider.packs[entry.name])
    return tuple(providers)


def _materialize_provider(
    entry: ToolPackEntry,
    context: RuntimeCapabilityContext | SessionCapabilityContext,
    *,
    allow_none: bool,
    module_loader: ModuleLoader,
) -> ToolProvider | None:
    module_name = entry.provider_factory_module
    if not module_name:
        raise CapabilityMaterializationError(entry.name, "builder_binding_missing")
    try:
        module = module_loader(module_name)
    except Exception as exc:  # noqa: BLE001 - report only the stable failure kind
        raise CapabilityMaterializationError(
            entry.name,
            f"builder_import_error:{type(exc).__name__}",
        ) from exc
    builder = getattr(module, CAPABILITY_PROVIDER_FACTORY, None)
    if not callable(builder):
        raise CapabilityMaterializationError(entry.name, "builder_export_missing")
    try:
        provider = builder(context)
    except CapabilityMaterializationError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the original exception as cause
        raise CapabilityMaterializationError(
            entry.name,
            f"builder_error:{type(exc).__name__}",
        ) from exc
    if provider is None and allow_none:
        return None
    if not isinstance(provider, ToolProvider):
        raise CapabilityMaterializationError(entry.name, "builder_result_invalid")
    if tuple(provider.packs) != (entry.name,):
        raise CapabilityMaterializationError(entry.name, "builder_pack_mismatch")
    return provider


__all__ = [
    "CapabilityMaterializationError",
    "RuntimeCapabilityContext",
    "SessionCapabilityContext",
    "materialize_runtime_providers",
    "materialize_session_providers",
]
