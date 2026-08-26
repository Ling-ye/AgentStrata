"""Single explicit registry for static and session-bound Agent tools."""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tool_validation import validate_tool_contract
from chatcopilot.contracts.tools import (
    ToolAudience,
    ToolDef,
    build_mcp_schema,
    build_openai_schema,
)
from chatcopilot.tool_packs.catalog import (
    BUILTIN_TOOL_PACKS,
    get_tool_pack_entry,
    known_tool_pack_names,
)


_PROVIDER_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_PACK_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
ModuleLoader = Callable[[str], ModuleType]


class ToolMaterializationError(RuntimeError):
    """A selected provider or tool contract could not be materialized safely."""

    def __init__(
        self,
        *,
        module: str,
        pack_names: Sequence[str],
        reason: str,
        tool_names: Sequence[str] = (),
        provider_id: str = "",
    ) -> None:
        self.module = module
        self.pack_names = tuple(pack_names)
        self.reason = reason
        self.tool_names = tuple(tool_names)
        self.provider_id = provider_id
        details = [f"module={module or '-'}", f"reason={reason}"]
        if provider_id:
            details.append(f"provider={provider_id}")
        if self.pack_names:
            details.append("packs=" + ",".join(self.pack_names))
        if self.tool_names:
            details.append("tools=" + ",".join(self.tool_names))
        super().__init__("tool materialization failed: " + "; ".join(details))


@dataclass(frozen=True)
class ToolSource:
    """Reviewable source locator for one materialized tool."""

    name: str
    provider_id: str
    pack_id: str
    provider_module: str
    handler_module: str
    handler_symbol: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "provider_id": self.provider_id,
            "pack_id": self.pack_id,
            "provider_module": self.provider_module,
            "handler_module": self.handler_module,
            "handler_symbol": self.handler_symbol,
        }


@dataclass(frozen=True, init=False)
class ToolRegistrySnapshot:
    """Immutable tool/index/schema projection used by all runtime consumers."""

    tools: tuple[ToolDef, ...]
    index: Mapping[str, ToolDef]
    sources: Mapping[str, ToolSource]
    openai_schema: tuple[Dict[str, object], ...]
    mcp_schema: tuple[Dict[str, object], ...]

    def __init__(self, tools: Sequence[ToolDef], sources: Mapping[str, ToolSource]) -> None:
        ordered_tools = tuple(tools)
        object.__setattr__(self, "tools", ordered_tools)
        object.__setattr__(
            self,
            "index",
            MappingProxyType({tool.name: tool for tool in ordered_tools}),
        )
        object.__setattr__(self, "sources", MappingProxyType(dict(sources)))
        object.__setattr__(
            self,
            "openai_schema",
            tuple(
                sorted(
                    (build_openai_schema(tool) for tool in ordered_tools),
                    key=lambda entry: str(
                        (entry.get("function") or {}).get("name") or ""
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "mcp_schema",
            tuple(
                sorted(
                    (build_mcp_schema(tool) for tool in ordered_tools),
                    key=lambda entry: str(entry.get("name") or ""),
                )
            ),
        )

    def describe(self, name: str) -> ToolSource | None:
        return self.sources.get(name)


class ToolRegistry:
    """Register providers once, then project selected packs into one snapshot."""

    def __init__(self, providers: Iterable[ToolProvider] = ()) -> None:
        self._providers: dict[str, ToolProvider] = {}
        self._pack_providers: dict[str, str] = {}
        for provider in providers:
            self.register_provider(provider)

    @classmethod
    def from_catalog(
        cls,
        tool_packs: Sequence[str] | None = None,
        *,
        module_loader: ModuleLoader = importlib.import_module,
    ) -> "ToolRegistry":
        """Load explicitly indexed static providers without scanning packages."""

        selected = (
            tuple(tool_packs)
            if tool_packs is not None
            else tuple(BUILTIN_TOOL_PACKS)
        )
        unknown = sorted(set(selected) - known_tool_pack_names())
        if unknown:
            raise ToolMaterializationError(
                module="chatcopilot.tool_packs.catalog",
                pack_names=unknown,
                reason="unknown_tool_pack",
            )

        registry = cls()
        loaded_modules: set[str] = set()
        for pack_id in selected:
            entry = get_tool_pack_entry(pack_id)
            if entry is None or entry.dynamic:
                continue
            module_path = entry.provider_module or ""
            if not module_path:
                raise ToolMaterializationError(
                    module="chatcopilot.tool_packs.catalog",
                    pack_names=(pack_id,),
                    reason="provider_module_missing",
                )
            if module_path not in loaded_modules:
                provider = _load_provider(module_path, module_loader=module_loader)
                registry.register_provider(provider, source_module=module_path)
                loaded_modules.add(module_path)
            if pack_id not in registry._pack_providers:
                raise ToolMaterializationError(
                    module=module_path,
                    pack_names=(pack_id,),
                    reason="provider_pack_missing",
                )
        return registry

    @property
    def providers(self) -> Mapping[str, ToolProvider]:
        return MappingProxyType(dict(self._providers))

    @property
    def pack_names(self) -> tuple[str, ...]:
        return tuple(self._pack_providers)

    def register_provider(
        self,
        provider: ToolProvider,
        *,
        source_module: str | None = None,
    ) -> None:
        """Register a static or already-materialized runtime provider."""

        if not isinstance(provider, ToolProvider):
            raise ToolMaterializationError(
                module=source_module or "",
                pack_names=(),
                reason="invalid_provider_export",
            )
        provider_id = provider.id
        if (
            not isinstance(provider_id, str)
            or provider_id != provider_id.strip()
            or _PROVIDER_ID_RE.fullmatch(provider_id) is None
        ):
            raise ToolMaterializationError(
                module=source_module or "",
                pack_names=tuple(provider.packs),
                reason="provider_id_invalid",
            )
        declared_module = provider.module
        if source_module and declared_module and declared_module != source_module:
            raise ToolMaterializationError(
                module=source_module,
                pack_names=tuple(provider.packs),
                reason="provider_module_mismatch",
                provider_id=provider_id,
            )
        module = source_module or declared_module
        if not isinstance(module, str) or not module.strip() or module != module.strip():
            raise ToolMaterializationError(
                module=str(module or ""),
                pack_names=tuple(provider.packs),
                reason="provider_module_missing",
                provider_id=provider_id,
            )
        if provider_id in self._providers:
            previous = self._providers[provider_id]
            raise ToolMaterializationError(
                module=module,
                pack_names=tuple(provider.packs),
                reason="duplicate_provider_id",
                provider_id=f"{previous.id},{provider_id}",
            )
        if not provider.packs:
            raise ToolMaterializationError(
                module=module,
                pack_names=(),
                reason="provider_packs_empty",
                provider_id=provider_id,
            )

        normalized_packs: dict[str, tuple[ToolDef, ...]] = {}
        for raw_pack_id, raw_tools in provider.packs.items():
            pack_id = raw_pack_id
            if (
                not isinstance(pack_id, str)
                or pack_id != pack_id.strip()
                or _PACK_ID_RE.fullmatch(pack_id) is None
            ):
                raise ToolMaterializationError(
                    module=module,
                    pack_names=(str(raw_pack_id),),
                    reason="provider_pack_id_invalid",
                    provider_id=provider_id,
                )
            previous_provider = self._pack_providers.get(pack_id)
            if previous_provider is not None:
                raise ToolMaterializationError(
                    module=module,
                    pack_names=(pack_id,),
                    reason="duplicate_pack_provider",
                    provider_id=f"{previous_provider},{provider_id}",
                )
            tools = tuple(raw_tools)
            if not tools:
                raise ToolMaterializationError(
                    module=module,
                    pack_names=(pack_id,),
                    reason="provider_pack_empty",
                    provider_id=provider_id,
                )
            seen_names: set[str] = set()
            for tool in tools:
                _validate_tool_contract(
                    tool,
                    module=module,
                    pack_id=pack_id,
                    provider_id=provider_id,
                )
                if tool.name in seen_names:
                    raise ToolMaterializationError(
                        module=module,
                        pack_names=(pack_id,),
                        reason="duplicate_tool_export",
                        tool_names=(tool.name,),
                        provider_id=provider_id,
                    )
                seen_names.add(tool.name)
            normalized_packs[pack_id] = tools

        normalized = ToolProvider(
            id=provider_id,
            packs=normalized_packs,
            module=module,
            description=provider.description,
        )
        self._providers[provider_id] = normalized
        for pack_id in normalized.packs:
            self._pack_providers[pack_id] = provider_id

    def register_runtime_provider(self, provider: ToolProvider) -> None:
        """Register one provider built from runtime or session dependencies."""

        self.register_provider(provider)

    def snapshot(
        self,
        *,
        tool_packs: Sequence[str] | None = None,
        exclude_tools: Sequence[str] | None = None,
        require_all_selected: bool = False,
        audience: ToolAudience | None = None,
    ) -> ToolRegistrySnapshot:
        """Project one selected, conflict-free tool surface."""

        selected = (
            tuple(tool_packs)
            if tool_packs is not None
            else tuple(self._pack_providers)
        )
        known = known_tool_pack_names() | set(self._pack_providers)
        unknown = sorted(set(selected) - known)
        if unknown:
            raise ToolMaterializationError(
                module="chatcopilot.tool_packs.catalog",
                pack_names=unknown,
                reason="unknown_tool_pack",
            )

        excluded = set(exclude_tools or ())
        tools: list[ToolDef] = []
        sources: dict[str, ToolSource] = {}
        tool_by_name: dict[str, ToolDef] = {}
        for pack_id in selected:
            provider_id = self._pack_providers.get(pack_id)
            if provider_id is None:
                entry = get_tool_pack_entry(pack_id)
                if entry is not None and entry.dynamic and not require_all_selected:
                    continue
                raise ToolMaterializationError(
                    module=(entry.provider_module if entry is not None else "") or "",
                    pack_names=(pack_id,),
                    reason="provider_not_registered",
                )
            provider = self._providers[provider_id]
            for tool in provider.packs[pack_id]:
                if tool.name in excluded:
                    continue
                if audience is not None and audience not in tool.audiences:
                    continue
                previous = tool_by_name.get(tool.name)
                if previous is not None:
                    previous_source = sources[tool.name]
                    if previous_source.provider_id == provider_id and (
                        previous is tool or previous == tool
                    ):
                        continue
                    raise ToolMaterializationError(
                        module=provider.module,
                        pack_names=(previous_source.pack_id, pack_id),
                        reason="duplicate_tool_name",
                        tool_names=(tool.name,),
                        provider_id=f"{previous_source.provider_id},{provider_id}",
                    )
                handler = tool.handler
                handler_module = str(getattr(handler, "__module__", "") or tool.module or "")
                handler_symbol = str(
                    getattr(handler, "__qualname__", "")
                    or getattr(handler, "__name__", "")
                    or type(handler).__qualname__
                )
                source = ToolSource(
                    name=tool.name,
                    provider_id=provider_id,
                    pack_id=pack_id,
                    provider_module=provider.module,
                    handler_module=handler_module,
                    handler_symbol=handler_symbol,
                )
                tool_by_name[tool.name] = tool
                sources[tool.name] = source
                tools.append(tool)
        return ToolRegistrySnapshot(tools, sources)

    def describe(
        self,
        name: str,
        *,
        tool_packs: Sequence[str] | None = None,
        exclude_tools: Sequence[str] | None = None,
        audience: ToolAudience | None = None,
    ) -> ToolSource | None:
        return self.snapshot(
            tool_packs=tool_packs,
            exclude_tools=exclude_tools,
            audience=audience,
        ).describe(name)


def _load_provider(module_path: str, *, module_loader: ModuleLoader) -> ToolProvider:
    try:
        module = module_loader(module_path)
    except Exception as exc:  # noqa: BLE001
        raise ToolMaterializationError(
            module=module_path,
            pack_names=(),
            reason=f"import_error:{type(exc).__name__}",
        ) from exc
    provider = getattr(module, "TOOL_PROVIDER", None)
    if not isinstance(provider, ToolProvider):
        raise ToolMaterializationError(
            module=module_path,
            pack_names=(),
            reason="missing_or_invalid_tool_provider_export",
        )
    return provider


def _validate_tool_contract(
    tool: object,
    *,
    module: str,
    pack_id: str,
    provider_id: str,
) -> None:
    if not isinstance(tool, ToolDef):
        raise ToolMaterializationError(
            module=module,
            pack_names=(pack_id,),
            reason="invalid_tool_export",
            tool_names=(type(tool).__name__,),
            provider_id=provider_id,
        )
    violations = validate_tool_contract(tool, require_provenance=False)
    if violations:
        raise ToolMaterializationError(
            module=module,
            pack_names=(pack_id,),
            reason=violations[0].materialization_reason,
            tool_names=(str(tool.name),),
            provider_id=provider_id,
        )


def discover_tools(
    *,
    tool_packs: Sequence[str] | None = None,
    exclude_tools: Sequence[str] | None = None,
    providers: Sequence[ToolProvider] = (),
) -> List[ToolDef]:
    registry = ToolRegistry.from_catalog(tool_packs)
    for provider in providers:
        registry.register_runtime_provider(provider)
    return list(
        registry.snapshot(
            tool_packs=tool_packs,
            exclude_tools=exclude_tools,
        ).tools
    )


def build_tools_schema(
    *,
    tool_packs: Sequence[str] | None = None,
    exclude_tools: Sequence[str] | None = None,
    providers: Sequence[ToolProvider] = (),
) -> Tuple[List[Dict[str, object]], Dict[str, ToolDef]]:
    registry = ToolRegistry.from_catalog(tool_packs)
    for provider in providers:
        registry.register_runtime_provider(provider)
    snapshot = registry.snapshot(
        tool_packs=tool_packs,
        exclude_tools=exclude_tools,
    )
    return list(snapshot.openai_schema), dict(snapshot.index)


def build_mcp_tools_schema(
    *,
    tool_packs: Sequence[str] | None = None,
    exclude_tools: Sequence[str] | None = None,
    providers: Sequence[ToolProvider] = (),
) -> Tuple[List[Dict[str, object]], Dict[str, ToolDef]]:
    registry = ToolRegistry.from_catalog(tool_packs)
    for provider in providers:
        registry.register_runtime_provider(provider)
    snapshot = registry.snapshot(
        tool_packs=tool_packs,
        exclude_tools=exclude_tools,
    )
    return list(snapshot.mcp_schema), dict(snapshot.index)


def find_spec(
    name: str,
    *,
    tool_packs: Sequence[str] | None = None,
    exclude_tools: Sequence[str] | None = None,
    providers: Sequence[ToolProvider] = (),
) -> Optional[ToolDef]:
    _schema, index = build_tools_schema(
        tool_packs=tool_packs,
        exclude_tools=exclude_tools,
        providers=providers,
    )
    return index.get(name)


__all__ = [
    "ToolMaterializationError",
    "ToolRegistry",
    "ToolRegistrySnapshot",
    "ToolSource",
    "build_mcp_tools_schema",
    "build_tools_schema",
    "discover_tools",
    "find_spec",
]
