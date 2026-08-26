"""Tool-pack, module, ToolDef, and prompt-policy catalog audits."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from types import ModuleType
from typing import cast

from chatcopilot.component_catalog.audit_models import (
    CatalogAuditIssue,
    CatalogRecords,
    ModuleLoader,
    _ToolPackAuditFacts,
    _append,
    _component_label,
    _records,
    _valid_component_id,
)
from chatcopilot.contracts.tool_validation import validate_tool_contract
from chatcopilot.contracts.tool_packs import (
    CAPABILITY_PROVIDER_FACTORY,
    ToolPackEntry,
    ToolPackPolicy,
    ToolProvider,
)
from chatcopilot.contracts.tools import ToolDef


_MODULE_RE = re.compile(
    r"^chatcopilot\.(?:"
    r"external_tools(?:\.[A-Za-z_][A-Za-z0-9_]*)+|"
    r"agent(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r")$"
)
_POLICY_MODULE_RE = re.compile(
    r"^chatcopilot\.external_tools(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_RUNTIME_SCOPES = frozenset({"static", "runtime", "agent_session", "host_session"})
_PROJECTION_PROFILES = frozenset({"interactive", "detached"})


class _ModuleCache:
    def __init__(self, loader: ModuleLoader) -> None:
        self._loader = loader
        self._modules: dict[str, ModuleType] = {}
        self._failures: dict[str, Exception] = {}

    def load(self, module: str) -> ModuleType:
        if module in self._modules:
            return self._modules[module]
        if module in self._failures:
            raise self._failures[module]
        try:
            loaded = self._loader(module)
        except Exception as exc:  # noqa: BLE001
            self._failures[module] = exc
            raise
        self._modules[module] = loaded
        return loaded


def _validate_tool(
    tool: ToolDef,
    *,
    module: str,
    issues: list[CatalogAuditIssue],
) -> str | None:
    issue_name = tool.name if isinstance(tool.name, str) else ""
    violations = validate_tool_contract(tool)
    for violation in violations:
        _append(
            issues,
            violation.audit_code,
            violation.message,
            surface="tool_pack",
            module=module,
            tool=issue_name,
        )
    if any(
        violation.audit_code == "tool.name_invalid"
        for violation in violations
    ):
        return None
    return tool.name


def _audit_policy(
    pack: str,
    entry: ToolPackEntry,
    *,
    modules: _ModuleCache,
    issues: list[CatalogAuditIssue],
) -> None:
    module_name = entry.policy_module
    if module_name is None:
        return
    if not isinstance(module_name, str) or _POLICY_MODULE_RE.fullmatch(module_name) is None:
        _append(
            issues,
            "policy.module_invalid",
            "Prompt policy modules must be inside chatcopilot.external_tools.",
            surface="tool_pack",
            component=pack,
            module=module_name if isinstance(module_name, str) else "",
        )
        return
    builder_name = entry.policy_builder
    if not isinstance(builder_name, str) or not builder_name.strip() or builder_name != builder_name.strip():
        _append(
            issues,
            "policy.builder_invalid",
            "Prompt policy builder names must be non-empty trimmed strings.",
            surface="tool_pack",
            component=pack,
            module=module_name,
        )
        return
    try:
        module = modules.load(module_name)
    except Exception as exc:  # noqa: BLE001
        _append(
            issues,
            "policy.import_failed",
            f"Prompt policy import failed: {type(exc).__name__}.",
            surface="tool_pack",
            component=pack,
            module=module_name,
        )
        return
    builder_value = getattr(module, builder_name, None)
    if not callable(builder_value):
        _append(
            issues,
            "policy.builder_missing",
            "Prompt policy builder is missing or not callable.",
            surface="tool_pack",
            component=pack,
            module=module_name,
        )
        return
    builder = cast(Callable[[], object], builder_value)
    mapping = getattr(module, "TOOL_PACK_POLICY_BUILDERS", None)
    if not isinstance(mapping, dict) or mapping.get(pack) is not builder:
        _append(
            issues,
            "policy.mapping_mismatch",
            "TOOL_PACK_POLICY_BUILDERS must map the pack id to its declared builder.",
            surface="tool_pack",
            component=pack,
            module=module_name,
        )
    try:
        policies = builder()
    except Exception as exc:  # noqa: BLE001
        _append(
            issues,
            "policy.build_failed",
            f"Prompt policy builder failed: {type(exc).__name__}.",
            surface="tool_pack",
            component=pack,
            module=module_name,
        )
        return
    if not isinstance(policies, tuple) or not policies or any(
        not isinstance(policy, ToolPackPolicy) for policy in policies
    ):
        _append(
            issues,
            "policy.result_invalid",
            "Policy builder must return a non-empty tuple of ToolPackPolicy.",
            surface="tool_pack",
            component=pack,
            module=module_name,
        )
        return
    policy_ids = tuple(policy.id for policy in policies)
    if len(policy_ids) != len(set(policy_ids)):
        _append(
            issues,
            "policy.ids_duplicate",
            "Tool pack policy ids must be unique within one pack.",
            surface="tool_pack",
            component=pack,
            module=module_name,
        )


def _audit_tool_packs(
    records: CatalogRecords,
    *,
    module_loader: ModuleLoader,
    issues: list[CatalogAuditIssue],
) -> _ToolPackAuditFacts:
    modules = _ModuleCache(module_loader)
    entries: list[tuple[str, ToolPackEntry]] = []
    packs_by_module: dict[str, list[tuple[str, ToolPackEntry]]] = defaultdict(list)

    for raw_key, raw_entry in _records(records):
        pack = _component_label(raw_key)
        if not _valid_component_id(raw_key):
            _append(
                issues,
                "tool_pack.key_invalid",
                "Tool-pack keys must use a stable lowercase component id.",
                surface="tool_pack",
                component=pack,
            )
        if not isinstance(raw_entry, ToolPackEntry):
            _append(
                issues,
                "tool_pack.entry_invalid",
                "Tool-pack values must be ToolPackEntry objects.",
                surface="tool_pack",
                component=pack,
            )
            continue
        entry = raw_entry
        entries.append((pack, entry))
        if entry.name != raw_key:
            _append(
                issues,
                "tool_pack.name_mismatch",
                "ToolPackEntry.name must match its catalog key.",
                surface="tool_pack",
                component=pack,
            )
        if not isinstance(entry.description, str) or not entry.description.strip():
            _append(
                issues,
                "tool_pack.description_invalid",
                "ToolPackEntry.description must be non-empty.",
                surface="tool_pack",
                component=pack,
            )
        if entry.runtime_scope not in _RUNTIME_SCOPES:
            _append(
                issues,
                "tool_pack.runtime_scope_invalid",
                "Tool-pack runtime scope must use the closed catalog values.",
                surface="tool_pack",
                component=pack,
            )
        if (
            not isinstance(entry.projection_profiles, tuple)
            or not entry.projection_profiles
            or len(entry.projection_profiles) != len(set(entry.projection_profiles))
            or not set(entry.projection_profiles) <= _PROJECTION_PROFILES
        ):
            _append(
                issues,
                "tool_pack.projection_profiles_invalid",
                "Tool-pack projection profiles must be a unique non-empty closed tuple.",
                surface="tool_pack",
                component=pack,
            )
        if entry.runtime_scope == "static" and entry.dynamic:
            _append(
                issues,
                "tool_pack.runtime_scope_mismatch",
                "Static-scope tool packs cannot be marked dynamic.",
                surface="tool_pack",
                component=pack,
            )
        if not isinstance(entry.session_default_enabled, bool) or (
            entry.session_default_enabled and entry.runtime_scope != "agent_session"
        ):
            _append(
                issues,
                "tool_pack.session_default_invalid",
                "Only Agent-session packs may be enabled by the compatibility default.",
                surface="tool_pack",
                component=pack,
            )
        builder_module = entry.provider_factory_module
        if entry.runtime_scope == "agent_session" and builder_module is None:
            _append(
                issues,
                "tool_pack.provider_factory_missing",
                "Agent-session packs require one trusted provider factory.",
                surface="tool_pack",
                component=pack,
            )
        if builder_module is not None and entry.runtime_scope in {
            "runtime",
            "agent_session",
        }:
            if (
                not isinstance(builder_module, str)
                or _MODULE_RE.fullmatch(builder_module) is None
                or not isinstance(entry.factory_order, int)
                or isinstance(entry.factory_order, bool)
                or entry.factory_order <= 0
            ):
                _append(
                    issues,
                    "tool_pack.provider_factory_invalid",
                    "Runtime factories require one trusted ordered callable binding.",
                    surface="tool_pack",
                    component=pack,
                    module=(builder_module if isinstance(builder_module, str) else ""),
                )
            else:
                try:
                    builder_module_value = modules.load(builder_module)
                except Exception as exc:  # noqa: BLE001
                    _append(
                        issues,
                        "tool_pack.provider_factory_import_failed",
                        f"Provider factory import failed: {type(exc).__name__}.",
                        surface="tool_pack",
                        component=pack,
                        module=builder_module,
                    )
                else:
                    if not callable(
                        getattr(
                            builder_module_value,
                            CAPABILITY_PROVIDER_FACTORY,
                            None,
                        )
                    ):
                        _append(
                            issues,
                            "tool_pack.provider_factory_export_invalid",
                            "Provider factory modules must export callable build_provider.",
                            surface="tool_pack",
                            component=pack,
                            module=builder_module,
                        )
        elif builder_module is not None:
            _append(
                issues,
                "tool_pack.provider_factory_unexpected",
                "Only runtime and Agent-session packs may declare provider factories.",
                surface="tool_pack",
                component=pack,
            )
        module_path = entry.provider_module
        if module_path is None:
            if not entry.dynamic:
                _append(
                    issues,
                    "tool_pack.provider_missing",
                    "Static tool packs must declare one provider module.",
                    surface="tool_pack",
                    component=pack,
                )
        elif not isinstance(module_path, str) or _MODULE_RE.fullmatch(module_path) is None:
            _append(
                issues,
                "tool_pack.provider_module_invalid",
                "Provider modules must be approved Agent or external-tool modules.",
                surface="tool_pack",
                component=pack,
                module=module_path if isinstance(module_path, str) else "",
            )
        elif not entry.dynamic:
            packs_by_module[module_path].append((pack, entry))
        if not isinstance(entry.http_route_modules, tuple) or any(
            not isinstance(module, str) or _POLICY_MODULE_RE.fullmatch(module) is None
            for module in entry.http_route_modules
        ):
            _append(
                issues,
                "tool_pack.http_routes_invalid",
                "HTTP route modules must be repository external-tool module paths.",
                surface="tool_pack",
                component=pack,
            )
        _audit_policy(pack, entry, modules=modules, issues=issues)

    locations: dict[str, set[str]] = defaultdict(set)
    declared_tools: set[str] = set()
    provider_ids: dict[str, str] = {}
    for module_name in sorted(packs_by_module):
        references = packs_by_module[module_name]
        try:
            loaded_module = modules.load(module_name)
        except Exception as exc:  # noqa: BLE001
            for pack, _entry_record in references:
                _append(
                    issues,
                    "tool_provider.import_failed",
                    f"Tool provider import failed: {type(exc).__name__}.",
                    surface="tool_pack",
                    component=pack,
                    module=module_name,
                )
            continue
        provider = getattr(loaded_module, "TOOL_PROVIDER", None)
        if not isinstance(provider, ToolProvider):
            _append(
                issues,
                "tool_provider.export_invalid",
                "Static provider modules must export one ToolProvider as TOOL_PROVIDER.",
                surface="tool_pack",
                module=module_name,
            )
            continue
        if provider.module != module_name:
            _append(
                issues,
                "tool_provider.module_mismatch",
                "ToolProvider.module must match its catalog module.",
                surface="tool_pack",
                module=module_name,
            )
        previous_module = provider_ids.get(provider.id)
        if previous_module is not None and previous_module != module_name:
            _append(
                issues,
                "tool_provider.id_conflict",
                "ToolProvider.id must be unique across provider modules.",
                surface="tool_pack",
                module=f"{previous_module},{module_name}",
            )
        provider_ids[provider.id] = module_name

        catalog_packs = {pack for pack, _entry_record in references}
        for extra_pack in sorted(set(provider.packs) - catalog_packs):
            _append(
                issues,
                "tool_provider.pack_unassigned",
                "Every provider-owned pack must have one catalog entry.",
                surface="tool_pack",
                component=extra_pack,
                module=module_name,
            )

        for pack, _entry_record in references:
            tools = provider.packs.get(pack)
            if not isinstance(tools, tuple) or not tools:
                _append(
                    issues,
                    "tool_provider.pack_missing",
                    "The provider must own a non-empty tuple for every catalog pack.",
                    surface="tool_pack",
                    component=pack,
                    module=module_name,
                )
                continue
            counts: Counter[str] = Counter()
            for index, tool in enumerate(tools):
                if not isinstance(tool, ToolDef):
                    _append(
                        issues,
                        "tool.type_invalid",
                        f"Provider pack item {index} must be a ToolDef.",
                        surface="tool_pack",
                        component=pack,
                        module=module_name,
                    )
                    continue
                valid_name = _validate_tool(tool, module=module_name, issues=issues)
                if valid_name is None:
                    continue
                counts[valid_name] += 1
                declared_tools.add(valid_name)
                locations[valid_name].add(module_name)
            for name, count in sorted(counts.items()):
                if count > 1:
                    _append(
                        issues,
                        "tool.name_duplicate_in_pack",
                        "A provider pack must not repeat a tool name.",
                        surface="tool_pack",
                        component=pack,
                        module=module_name,
                        tool=name,
                    )

    for name, module_names in sorted(locations.items()):
        if len(module_names) > 1:
            _append(
                issues,
                "tool.name_conflict",
                "A static tool name must be owned by exactly one module.",
                surface="tool_pack",
                module=",".join(sorted(module_names)),
                tool=name,
            )

    return _ToolPackAuditFacts(
        pack_count=len(entries),
        module_count=len(packs_by_module),
        tool_names=frozenset(declared_tools),
    )
