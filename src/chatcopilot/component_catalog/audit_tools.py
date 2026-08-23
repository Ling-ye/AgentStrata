"""Tool-pack, module, ToolDef, and prompt-policy catalog audits."""

from __future__ import annotations

import inspect
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from types import ModuleType
from typing import cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

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
    ToolPackEntry,
    ToolPackPolicy,
    ToolProvider,
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
    r"agent(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
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

    input_schema = tool.input_schema
    output_schema = tool.output_schema
    schemas_valid = True
    for schema_name, schema in (
        ("input", input_schema),
        ("output", output_schema),
    ):
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schemas_valid = False
            _append(
                issues,
                f"tool.{schema_name}_schema_invalid",
                f"ToolDef.{schema_name}_schema must be an object JSON schema.",
                surface=surface,
                module=module,
                tool=issue_name,
            )
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            schemas_valid = False
            _append(
                issues,
                f"tool.{schema_name}_schema_invalid",
                f"ToolDef.{schema_name}_schema must be a valid JSON schema.",
                surface=surface,
                module=module,
                tool=issue_name,
            )

    properties = (
        input_schema.get("properties", {}) if isinstance(input_schema, dict) else None
    )
    properties_valid = isinstance(properties, dict)
    if not properties_valid:
        _append(
            issues,
            "tool.properties_invalid",
            "ToolDef.input_schema.properties must be a dict.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    else:
        for property_name, schema in properties.items():
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

    required = input_schema.get("required", []) if isinstance(input_schema, dict) else None
    if not isinstance(required, list):
        _append(
            issues,
            "tool.required_invalid",
            "ToolDef.input_schema.required must be a list.",
            surface=surface,
            module=module,
            tool=issue_name,
        )
    else:
        string_required = [item for item in required if isinstance(item, str)]
        if len(string_required) != len(required) or any(
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
            item not in properties for item in string_required
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
    else:
        try:
            parameters = tuple(inspect.signature(tool.handler).parameters.values())
        except (TypeError, ValueError):
            parameters = ()
        if len(parameters) != 2 or any(
            parameter.kind
            not in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            for parameter in parameters
        ):
            _append(
                issues,
                "tool.handler_signature_invalid",
                "ToolDef.handler must accept exactly (arguments, ToolContext).",
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
    if not schemas_valid or not schemas or not _json_serializable(schemas):
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
