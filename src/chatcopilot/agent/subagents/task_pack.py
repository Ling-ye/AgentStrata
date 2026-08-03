"""Structured task pack passed from the main agent to a subagent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    legacy_task: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {f: getattr(self, f) for f in TASK_PACK_FIELDS}
        if self.extra:
            data.update(self.extra)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


_KNOWN_KEYS = {
    "objective", "user_intent", "deliverable", "constraints", "inputs",
    "resources", "acceptance_criteria", "evidence_required", "write_scope",
    "domain", "target_sites", "time_window", "required_fields", "cross_check",
    "excluded_context", "cache_key_hint", "task", "workflow_depth",
}


def parse_task_pack(args: Mapping[str, Any] | None) -> TaskPack:
    """Normalize delegate tool arguments into a task pack.

    The legacy ``task`` argument remains accepted and is mapped to
    ``objective`` so existing prompts/tests keep working while the new schema is
    available to main agents.  Unknown keys (from ``input_schema``) are collected
    into :attr:`TaskPack.extra`.
    """

    args = args or {}
    legacy_task = str(args.get("task") or "").strip()
    objective = str(args.get("objective") or legacy_task).strip()
    if not objective:
        raise ValueError("objective cannot be empty")

    extra = {k: v for k, v in args.items() if k not in _KNOWN_KEYS}

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
        legacy_task=legacy_task,
        extra=extra,
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
        "task": {
            "type": "string",
            "description": "Deprecated compatibility alias for objective.",
        },
    }
    for name in _LIST_FIELDS:
        if name in _SEARCH_FIELDS:
            continue
        fields[name] = {"type": "array", "items": {"type": "string"}}
    return fields


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list | tuple):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["TaskPack", "parse_task_pack", "task_pack_schema"]
