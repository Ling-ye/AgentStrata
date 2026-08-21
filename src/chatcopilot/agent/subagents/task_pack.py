"""Structured task pack passed from the main agent to a subagent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from chatcopilot.agent.subagents.spec import TASK_PACK_FIELDS


_LIST_FIELDS = {
    "constraints",
    "inputs",
    "resources",
    "acceptance_criteria",
    "evidence_required",
    "excluded_context",
    "target_sites",
    "required_fields",
}

_SEARCH_FIELDS = {
    "domain",
    "target_sites",
    "time_window",
    "required_fields",
    "cross_check",
}


@dataclass(frozen=True)
class TaskPack:
    objective: str
    user_intent: str = ""
    deliverable: str = ""
    constraints: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    evidence_required: tuple[str, ...] = ()
    domain: str = ""
    target_sites: tuple[str, ...] = ()
    time_window: str = ""
    required_fields: tuple[str, ...] = ()
    cross_check: bool = False
    write_scope: str = ""
    excluded_context: tuple[str, ...] = ()
    cache_key_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in TASK_PACK_FIELDS}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


_KNOWN_KEYS = {
    "objective", "user_intent", "deliverable", "constraints", "inputs",
    "resources", "acceptance_criteria", "evidence_required", "write_scope",
    "domain", "target_sites", "time_window", "required_fields", "cross_check",
    "excluded_context", "cache_key_hint",
}


def parse_task_pack(args: Mapping[str, Any] | None) -> TaskPack:
    """Parse the strict TaskPack schema without aliases or unknown fields."""

    args = args or {}
    unknown = sorted(set(args) - _KNOWN_KEYS)
    if unknown:
        raise ValueError("unsupported TaskPack field(s): " + ", ".join(unknown))
    raw_objective = args.get("objective")
    if raw_objective is not None and not isinstance(raw_objective, str):
        raise TypeError("objective must be a string")
    objective = str(raw_objective or "").strip()
    if not objective:
        raise ValueError("objective cannot be empty")

    return TaskPack(
        objective=objective,
        user_intent=str(args.get("user_intent") or "").strip(),
        deliverable=str(args.get("deliverable") or "").strip(),
        constraints=_as_tuple(args.get("constraints")),
        inputs=_as_tuple(args.get("inputs")),
        resources=_as_tuple(args.get("resources")),
        acceptance_criteria=_as_tuple(args.get("acceptance_criteria")),
        evidence_required=_as_tuple(args.get("evidence_required")),
        domain=str(args.get("domain") or "").strip(),
        target_sites=_as_tuple(args.get("target_sites")),
        time_window=str(args.get("time_window") or "").strip(),
        required_fields=_as_tuple(args.get("required_fields")),
        cross_check=_as_bool(args.get("cross_check")),
        write_scope=str(args.get("write_scope") or "").strip(),
        excluded_context=_as_tuple(args.get("excluded_context")),
        cache_key_hint=str(args.get("cache_key_hint") or "").strip(),
    )


def task_pack_schema() -> dict[str, Any]:
    fields: dict[str, Any] = {
        "objective": {
            "type": "string",
            "description": "Specific objective for this subagent. Required.",
        },
        "user_intent": {"type": "string"},
        "deliverable": {"type": "string"},
        "write_scope": {
            "type": "string",
            "description": "Allowed mutation scope when write-capable tools are delegated.",
        },
        "cache_key_hint": {"type": "string"},
    }
    for name in _LIST_FIELDS:
        if name in _SEARCH_FIELDS:
            continue
        fields[name] = {"type": "array", "items": {"type": "string"}}
    return fields


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        if any(not isinstance(item, str) for item in value):
            raise TypeError("TaskPack array fields must contain strings")
        return tuple(item.strip() for item in value if item.strip())
    raise TypeError("TaskPack array fields must be arrays of strings")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    raise TypeError("cross_check must be a boolean")


__all__ = ["TaskPack", "parse_task_pack", "task_pack_schema"]
