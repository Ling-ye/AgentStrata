"""Pure validation for static and runtime ``ToolDef`` values."""
from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from chatcopilot.contracts.tools import (
    EXECUTION_GLOBAL_SERIAL_BACKGROUND,
    EXECUTION_SYNC,
    EXECUTION_USER_SERIAL_BACKGROUND,
    TOOL_AUDIENCES,
    ToolDef,
    build_mcp_schema,
    build_openai_schema,
)


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
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
_AUDIENCES = frozenset(TOOL_AUDIENCES)


@dataclass(frozen=True)
class ToolContractViolation:
    audit_code: str
    materialization_reason: str
    message: str


def _json_serializable(value: object) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    return True


def validate_tool_contract(
    tool: ToolDef,
    *,
    require_provenance: bool = True,
) -> tuple[ToolContractViolation, ...]:
    """Return every deterministic contract violation without invoking the handler."""

    violations: list[ToolContractViolation] = []

    def add(audit_code: str, reason: str, message: str) -> None:
        violations.append(
            ToolContractViolation(
                audit_code=audit_code,
                materialization_reason=reason,
                message=message,
            )
        )

    name = tool.name
    if not isinstance(name, str) or _TOOL_NAME_RE.fullmatch(name) is None:
        add(
            "tool.name_invalid",
            "invalid_tool_name",
            "ToolDef.name must match [A-Za-z0-9_-]{1,64}.",
        )
    if not isinstance(tool.summary, str) or not tool.summary.strip():
        add(
            "tool.summary_invalid",
            "invalid_tool_summary",
            "ToolDef.summary must be a non-empty string.",
        )

    schemas_valid = True
    for schema_name, schema in (
        ("input", tool.input_schema),
        ("output", tool.output_schema),
    ):
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schemas_valid = False
            add(
                f"tool.{schema_name}_schema_invalid",
                f"invalid_{schema_name}_schema",
                f"ToolDef.{schema_name}_schema must be an object JSON schema.",
            )
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except (SchemaError, TypeError):
            schemas_valid = False
            add(
                f"tool.{schema_name}_schema_invalid",
                f"invalid_{schema_name}_schema",
                f"ToolDef.{schema_name}_schema must be a valid JSON schema.",
            )

    input_schema = tool.input_schema
    properties = (
        input_schema.get("properties", {}) if isinstance(input_schema, dict) else None
    )
    properties_valid = isinstance(properties, dict)
    if not properties_valid:
        add(
            "tool.properties_invalid",
            "invalid_tool_input_properties",
            "ToolDef.input_schema.properties must be a dict.",
        )
    else:
        for property_name, schema in properties.items():
            if (
                not isinstance(property_name, str)
                or not property_name.strip()
                or property_name != property_name.strip()
            ):
                add(
                    "tool.property_name_invalid",
                    "invalid_tool_property_name",
                    "Tool property names must be non-empty trimmed strings.",
                )
            if not isinstance(schema, dict):
                add(
                    "tool.property_schema_invalid",
                    "invalid_tool_property_schema",
                    "Every ToolDef property schema must be a dict.",
                )

    required = input_schema.get("required", []) if isinstance(input_schema, dict) else None
    if not isinstance(required, list):
        add(
            "tool.required_invalid",
            "invalid_tool_required",
            "ToolDef.input_schema.required must be a list.",
        )
    else:
        string_required = [item for item in required if isinstance(item, str)]
        if len(string_required) != len(required) or any(
            not item.strip() or item != item.strip() for item in string_required
        ):
            add(
                "tool.required_name_invalid",
                "invalid_tool_required_name",
                "Required property names must be non-empty trimmed strings.",
            )
        if len(set(string_required)) != len(string_required):
            add(
                "tool.required_duplicate",
                "duplicate_tool_required_name",
                "ToolDef.required must not contain duplicate names.",
            )
        if properties_valid and any(item not in properties for item in string_required):
            add(
                "tool.required_unknown",
                "unknown_tool_required_name",
                "Every required property must exist in ToolDef.properties.",
            )

    if not callable(tool.handler):
        add(
            "tool.handler_invalid",
            "invalid_tool_handler",
            "ToolDef.handler must be callable.",
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
            add(
                "tool.handler_signature_invalid",
                "invalid_tool_handler_signature",
                "ToolDef.handler must accept exactly (arguments, ToolContext).",
            )

    if not (
        tool.requires_role is None
        or isinstance(tool.requires_role, str)
        and tool.requires_role in _ROLES
    ):
        add(
            "tool.requires_role_invalid",
            "invalid_tool_requires_role",
            "ToolDef.requires_role must be None, user, admin, or owner.",
        )
    if (
        not isinstance(tool.execution_policy, str)
        or tool.execution_policy not in _EXECUTION_POLICIES
    ):
        add(
            "tool.execution_policy_invalid",
            "invalid_tool_execution_policy",
            "ToolDef.execution_policy is not supported.",
        )
    if not isinstance(tool.weight, str) or tool.weight not in _WEIGHTS:
        add(
            "tool.weight_invalid",
            "invalid_tool_weight",
            "ToolDef.weight must be light or heavy.",
        )
    if (
        not isinstance(tool.category, str)
        or tool.category != tool.category.strip()
        or require_provenance
        and not tool.category
    ):
        add(
            "tool.category_invalid",
            "invalid_tool_category",
            "ToolDef.category must be a non-empty string.",
        )
    if (
        not isinstance(tool.owner, str)
        or tool.owner != tool.owner.strip()
        or require_provenance
        and not tool.owner
    ):
        add(
            "tool.owner_invalid",
            "invalid_tool_owner",
            "ToolDef.owner must be a non-empty string.",
        )
    if (
        not isinstance(tool.module, str)
        or tool.module != tool.module.strip()
    ):
        add(
            "tool.module_invalid",
            "invalid_tool_module",
            "ToolDef.module must be a trimmed string.",
        )
    if not isinstance(tool.aliases, list) or any(
        not isinstance(alias, str) or not alias.strip() or alias != alias.strip()
        for alias in tool.aliases
    ):
        add(
            "tool.aliases_invalid",
            "invalid_tool_aliases",
            "ToolDef.aliases must be a list of non-empty trimmed strings.",
        )
    elif len(set(tool.aliases)) != len(tool.aliases):
        add(
            "tool.alias_duplicate",
            "duplicate_tool_alias",
            "ToolDef.aliases must not contain duplicates.",
        )
    if (
        not isinstance(tool.audiences, tuple)
        or not tool.audiences
        or any(
            not isinstance(audience, str) or audience not in _AUDIENCES
            for audience in tool.audiences
        )
        or len(set(tool.audiences)) != len(tool.audiences)
    ):
        add(
            "tool.audiences_invalid",
            "invalid_tool_audiences",
            "ToolDef.audiences must be a non-empty unique tuple of main or subagent.",
        )
    if not isinstance(tool.artifact_kinds, tuple) or any(
        not isinstance(kind, str) or kind not in _ARTIFACT_KINDS
        for kind in tool.artifact_kinds
    ):
        add(
            "tool.artifact_kinds_invalid",
            "invalid_tool_artifact_kinds",
            "ToolDef.artifact_kinds must contain only file or directory.",
        )
    if not isinstance(tool.metadata, dict) or not _json_serializable(tool.metadata):
        add(
            "tool.metadata_invalid",
            "invalid_tool_metadata",
            "ToolDef.metadata must be a JSON-serializable dict.",
        )

    schemas: tuple[object, ...]
    try:
        schemas = (build_openai_schema(tool), build_mcp_schema(tool))
    except Exception:  # noqa: BLE001
        schemas = ()
    if not schemas_valid or not schemas or not _json_serializable(schemas):
        add(
            "tool.schema_invalid",
            "invalid_tool_schema_projection",
            "OpenAI and MCP schemas must be finite JSON-serializable values.",
        )
    return tuple(violations)


__all__ = ["ToolContractViolation", "validate_tool_contract"]
