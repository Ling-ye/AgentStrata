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
    _TOOL_NAME_RE,
    _ToolPackAuditFacts,
    _append,
    _component_label,
    _json_serializable,
    _records,
    _valid_component_id,
)
from chatcopilot.contracts.tool_packs import (
    ToolModuleBinding,
    ToolPackEntry,
    ToolPackPolicy,
)
from chatcopilot.contracts.tools import (
    EXECUTION_GLOBAL_SERIAL_BACKGROUND,
    EXECUTION_SYNC,
    EXECUTION_USER_SERIAL_BACKGROUND,
    ToolDef,
    build_mcp_schema,
    build_openai_schema,
)


_MODULE_RE = re.compile(
    r"^chatcopilot\.(?:"
    r"external_tools(?:\.[A-Za-z_][A-Za-z0-9_]*)+|"
    r"agent\.tools\.builtin(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r")$"
)
_POLICY_MODULE_RE = re.compile(
    r"^chatcopilot\.external_tools(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_ROLES = frozenset({None, "user", "admin", "owner"})
_EXECUTION_POLICIES = frozenset(
    {
        EXECUTION_SYNC,
        EXECUTION_GLOBAL_SERIAL_BACKGROUND,
        EXECUTION_USER_SERIAL_BACKGROUND,
    }
)
_WEIGHTS = frozenset({"light", "heavy"})
_ARTIFACT_KINDS = frozenset({"file", "directory"})


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
    surface = "tool_pack"
    name = tool.name
    valid_name = isinstance(name, str) and _TOOL_NAME_RE.fullmatch(name) is not None
    issue_name = name if isinstance(name, str) else ""
    if not valid_name:
        _append(
            issues,
            "tool.name_invalid",
            "ToolDef.name must match [A-Za-z0-9_-]{1,64}.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not isinstance(tool.summary, str) or not tool.summary.strip():
        _append(
            issues,
            "tool.summary_invalid",
            "ToolDef.summary must be a non-empty string.",
            surface=surface,
            module=module,
            tool=issue_name,
        )

    properties_valid = isinstance(tool.properties, dict)
    if not properties_valid:
        _append(
            issues,
            "tool.properties_invalid",
            "ToolDef.properties must be a dict.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    else:
        for property_name, schema in tool.properties.items():
            if (
                not isinstance(property_name, str)
                or not property_name.strip()
                or property_name != property_name.strip()
            ):
                _append(
                    issues,
                    "tool.property_name_invalid",
                    "Tool property names must be non-empty trimmed strings.",
                    surface=surface,
                    module=module,
                    tool=issue_name,
                )
            if not isinstance(schema, dict):
                _append(
                    issues,
                    "tool.property_schema_invalid",
                    "Every ToolDef property schema must be a dict.",
                    surface=surface,
                    module=module,
                    tool=issue_name,
                )

    if not isinstance(tool.required, list):
        _append(
            issues,
            "tool.required_invalid",
            "ToolDef.required must be a list.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    else:
        string_required = [item for item in tool.required if isinstance(item, str)]
        if len(string_required) != len(tool.required) or any(
            not item.strip() or item != item.strip() for item in string_required
        ):
            _append(
                issues,
                "tool.required_name_invalid",
                "Required property names must be non-empty trimmed strings.",
                surface=surface,
                module=module,
                tool=issue_name,
            )
        if len(set(string_required)) != len(string_required):
            _append(
                issues,
                "tool.required_duplicate",
                "ToolDef.required must not contain duplicate names.",
                surface=surface,
                module=module,
                tool=issue_name,
            )
        if properties_valid and any(
            required not in tool.properties for required in string_required
        ):
            _append(
                issues,
                "tool.required_unknown",
                "Every required property must exist in ToolDef.properties.",
                surface=surface,
                module=module,
                tool=issue_name,
            )

    if not callable(tool.handler):
        _append(
            issues,
            "tool.handler_invalid",
            "ToolDef.handler must be callable.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not (
        tool.requires_role is None
        or isinstance(tool.requires_role, str)
        and tool.requires_role in _ROLES
    ):
        _append(
            issues,
            "tool.requires_role_invalid",
            "ToolDef.requires_role must be None, user, admin, or owner.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not isinstance(tool.execution_policy, str) or (
        tool.execution_policy not in _EXECUTION_POLICIES
    ):
        _append(
            issues,
            "tool.execution_policy_invalid",
            "ToolDef.execution_policy is not supported.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not isinstance(tool.weight, str) or tool.weight not in _WEIGHTS:
        _append(
            issues,
            "tool.weight_invalid",
            "ToolDef.weight must be light or heavy.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not isinstance(tool.category, str) or not tool.category.strip():
        _append(
            issues,
            "tool.category_invalid",
            "ToolDef.category must be a non-empty string.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not isinstance(tool.owner, str) or not tool.owner.strip():
        _append(
            issues,
            "tool.owner_invalid",
            "ToolDef.owner must be a non-empty string.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not isinstance(tool.aliases, list) or any(
        not isinstance(alias, str) or not alias.strip() or alias != alias.strip()
        for alias in tool.aliases
    ):
        _append(
            issues,
            "tool.aliases_invalid",
            "ToolDef.aliases must be a list of non-empty trimmed strings.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    elif len(set(tool.aliases)) != len(tool.aliases):
        _append(
            issues,
            "tool.alias_duplicate",
            "ToolDef.aliases must not contain duplicates.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not isinstance(tool.artifact_kinds, tuple) or any(
        not isinstance(kind, str) or kind not in _ARTIFACT_KINDS
        for kind in tool.artifact_kinds
    ):
        _append(
            issues,
            "tool.artifact_kinds_invalid",
            "ToolDef.artifact_kinds must contain only file or directory.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    if not isinstance(tool.metadata, dict) or not _json_serializable(tool.metadata):
        _append(
            issues,
            "tool.metadata_invalid",
            "ToolDef.metadata must be a JSON-serializable dict.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    schemas: tuple[object, ...]
    try:
        schemas = (build_openai_schema(tool), build_mcp_schema(tool))
    except Exception:  # noqa: BLE001
        schemas = ()
    if not schemas or not _json_serializable(schemas):
        _append(
            issues,
            "tool.schema_invalid",
            "OpenAI and MCP schemas must be finite JSON-serializable values.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    return name if valid_name else None


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
    bindings_by_module: dict[str, list[tuple[str, ToolModuleBinding]]] = defaultdict(list)
    pack_name_counts: dict[str, Counter[str]] = defaultdict(Counter)

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
        if not isinstance(entry.tool_bindings, tuple):
            _append(
                issues,
                "tool_pack.bindings_invalid",
                "ToolPackEntry.tool_bindings must be a tuple.",
                surface="tool_pack",
                component=pack,
            )
        else:
            seen_modules: set[str] = set()
            for binding in entry.tool_bindings:
                if not isinstance(binding, ToolModuleBinding):
                    _append(
                        issues,
                        "tool_binding.type_invalid",
                        "Every tool binding must be ToolModuleBinding.",
                        surface="tool_pack",
                        component=pack,
                    )
                    continue
                module_path = binding.module
                if not isinstance(module_path, str) or _MODULE_RE.fullmatch(module_path) is None:
                    _append(
                        issues,
                        "tool_binding.module_invalid",
                        "Tool modules must be approved Agent builtin or external-tool modules.",
                        surface="tool_pack",
                        component=pack,
                        module=module_path if isinstance(module_path, str) else "",
                    )
                    continue
                if module_path in seen_modules:
                    _append(
                        issues,
                        "tool_binding.module_duplicate",
                        "A pack must declare each tool module once.",
                        surface="tool_pack",
                        component=pack,
                        module=module_path,
                    )
                seen_modules.add(module_path)
                if not isinstance(binding.tool_names, tuple) or not binding.tool_names:
                    _append(
                        issues,
                        "tool_binding.names_invalid",
                        "ToolModuleBinding.tool_names must be a non-empty tuple.",
                        surface="tool_pack",
                        component=pack,
                        module=module_path,
                    )
                    continue
                valid_names: list[str] = []
                for name in binding.tool_names:
                    if not isinstance(name, str) or _TOOL_NAME_RE.fullmatch(name) is None:
                        _append(
                            issues,
                            "tool_binding.name_invalid",
                            "Declared tool names must match [A-Za-z0-9_-]{1,64}.",
                            surface="tool_pack",
                            component=pack,
                            module=module_path,
                            tool=name if isinstance(name, str) else "",
                        )
                        continue
                    valid_names.append(name)
                    pack_name_counts[pack][name] += 1
                if len(set(valid_names)) != len(valid_names):
                    _append(
                        issues,
                        "tool_binding.name_duplicate",
                        "A module binding must not repeat a tool name.",
                        surface="tool_pack",
                        component=pack,
                        module=module_path,
                    )
                bindings_by_module[module_path].append((pack, binding))
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

    for pack, counts in sorted(pack_name_counts.items()):
        for name, count in sorted(counts.items()):
            if count > 1:
                _append(
                    issues,
                    "tool_pack.projection_duplicate",
                    "One pack projects the same tool through multiple bindings.",
                    surface="tool_pack",
                    component=pack,
                    tool=name,
                )

    locations: dict[str, set[str]] = defaultdict(set)
    declared_tools: set[str] = set()
    for module_name in sorted(bindings_by_module):
        references = bindings_by_module[module_name]
        try:
            loaded_module = modules.load(module_name)
        except Exception as exc:  # noqa: BLE001
            for pack, _binding_record in references:
                _append(
                    issues,
                    "tool_module.import_failed",
                    f"Tool module import failed: {type(exc).__name__}.",
                    surface="tool_pack",
                    component=pack,
                    module=module_name,
                )
            continue
        exported = getattr(loaded_module, "TOOLS", None)
        if not isinstance(exported, (list, tuple)) or not exported:
            _append(
                issues,
                "tool_module.export_invalid",
                "Tool modules must export a non-empty list or tuple named TOOLS.",
                surface="tool_pack",
                module=module_name,
            )
            continue
        exported_counts: Counter[str] = Counter()
        exported_names: set[str] = set()
        for index, tool in enumerate(exported):
            if not isinstance(tool, ToolDef):
                _append(
                    issues,
                    "tool.type_invalid",
                    f"TOOLS[{index}] must be a ToolDef.",
                    surface="tool_pack",
                    module=module_name,
                )
                continue
            valid_name = _validate_tool(tool, module=module_name, issues=issues)
            if valid_name is None:
                continue
            exported_counts[valid_name] += 1
            exported_names.add(valid_name)
            locations[valid_name].add(module_name)
        for name, count in sorted(exported_counts.items()):
            if count > 1:
                _append(
                    issues,
                    "tool.name_duplicate_in_module",
                    "A module must not export the same tool name more than once.",
                    surface="tool_pack",
                    module=module_name,
                    tool=name,
                )

        declared_in_module = {
            name
            for _pack, binding in references
            for name in binding.tool_names
            if isinstance(name, str)
        }
        declared_tools.update(declared_in_module & exported_names)
        for pack, binding in references:
            for name in binding.tool_names:
                if isinstance(name, str) and name not in exported_names:
                    _append(
                        issues,
                        "tool_binding.tool_missing",
                        "A declared tool name is not exported by its bound module.",
                        surface="tool_pack",
                        component=pack,
                        module=module_name,
                        tool=name,
                    )
        for name in sorted(exported_names - declared_in_module):
            _append(
                issues,
                "tool_module.tool_unassigned",
                "Every exported ToolDef must belong to at least one catalog pack.",
                surface="tool_pack",
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
        module_count=len(bindings_by_module),
        tool_names=frozenset(declared_tools),
    )
