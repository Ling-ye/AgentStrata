"""Structured subagent result and the mandatory submit_result tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef

SUBMIT_RESULT_TOOL = "submit_result"


@dataclass
class SubagentResultHolder:
    payload: Dict[str, Any] | None = None
    outputs: List[str] = field(default_factory=list)


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [str(item) for item in value if str(item).strip()]
    return []


def _as_jsonish_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [item for item in value if str(item).strip()]
    return [value]


def _normalize_evidence(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(value, Sequence) or isinstance(value, str):
        return out
    for item in value:
        if isinstance(item, dict):
            claim = str(item.get("claim") or item.get("text") or "").strip()
            source = str(item.get("source") or item.get("url") or "").strip()
            if claim or source:
                out.append({"claim": claim, "source": source})
        elif str(item).strip():
            out.append({"claim": str(item).strip(), "source": ""})
    return out


def build_submit_result_tool(holder: SubagentResultHolder) -> ToolDef:
    def _handler(args: Dict[str, Any]) -> HandlerResult:
        args = args or {}
        summary = str(args.get("summary") or "").strip()
        if not summary:
            raise ValueError("submit_result.summary cannot be empty")
        outputs = _as_str_list(args.get("outputs"))
        limits = args.get("limits")
        payload: Dict[str, Any] = {
            "ok": bool(args.get("ok", True)),
            "error_code": str(args.get("error_code") or "").strip(),
            "summary": summary,
            "findings": _as_jsonish_list(args.get("findings")),
            "evidence": _normalize_evidence(args.get("evidence")),
            "changes": _as_jsonish_list(args.get("changes")),
            "commands_run": _as_jsonish_list(args.get("commands_run")),
            "outputs": outputs,
            "risks": _as_jsonish_list(args.get("risks")),
            "limits": limits if isinstance(limits, dict) else {},
            "next_steps": _as_str_list(args.get("next_steps")),
            "confidence": str(args.get("confidence") or "").strip(),
            "cache_summary": str(args.get("cache_summary") or "").strip(),
        }
        holder.payload = payload
        holder.outputs = outputs
        return ("structured result submitted", outputs, None)

    return ToolDef(
        name=SUBMIT_RESULT_TOOL,
        summary="Submit the final structured result for this subagent task.",
        properties={
            "summary": {
                "type": "string",
                "description": "Concise conclusion for the main agent.",
            },
            "findings": {
                "type": "array",
                "description": "Atomic findings for the main agent.",
                "items": {"type": "object"},
            },
            "evidence": {
                "type": "array",
                "description": "Evidence items with claim and source.",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "source": {"type": "string"},
                    },
                },
            },
            "changes": {
                "type": "array",
                "description": "Files, settings, or external state changed.",
                "items": {"type": "object"},
            },
            "commands_run": {
                "type": "array",
                "description": "Commands or tool actions executed.",
                "items": {"type": "object"},
            },
            "outputs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Artifact file or directory paths for the main agent to handle.",
            },
            "risks": {
                "type": "array",
                "description": "Risks, uncertainties, and review concerns.",
                "items": {"type": "object"},
            },
            "limits": {
                "type": "object",
                "description": "Limits, uncertainty, and uncovered scope.",
            },
            "next_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Suggested next steps for the main agent.",
            },
            "confidence": {
                "type": "string",
                "description": "low, medium, or high confidence in the result.",
            },
            "cache_summary": {
                "type": "string",
                "description": "Whether the result was produced from cache or is cacheable.",
            },
            "ok": {
                "type": "boolean",
                "description": "Whether the delegated task reached its goal.",
                "default": True,
            },
            "error_code": {
                "type": "string",
                "description": "Stable upstream error code when ok is false.",
            },
        },
        required=["summary"],
        handler=_handler,
        category="agent.subagent",
        owner="agent",
        module=__name__,
        metadata={"subagent_internal": True},
    )


def build_result_payload(
    *,
    holder: SubagentResultHolder,
    final_text: str,
    ok: bool,
    error_code: str | None,
    max_chars: int,
) -> Dict[str, Any]:
    if holder.payload is not None:
        payload = dict(holder.payload)
        payload.setdefault("limits", {})
    else:
        payload = {
            "ok": ok,
            "summary": (final_text or "").strip(),
            "findings": [],
            "evidence": [],
            "changes": [],
            "commands_run": [],
            "outputs": [],
            "risks": [],
            "limits": {"partial": True, "reason": "subagent did not call submit_result"},
            "next_steps": [],
            "confidence": "low",
            "cache_summary": "not_used",
        }
    for key, default in (
        ("findings", []),
        ("evidence", []),
        ("changes", []),
        ("commands_run", []),
        ("outputs", []),
        ("risks", []),
        ("next_steps", []),
        ("confidence", ""),
        ("cache_summary", ""),
    ):
        payload.setdefault(key, default)

    if error_code:
        payload["ok"] = False
        payload["error_code"] = error_code
        if isinstance(payload.get("limits"), dict):
            payload["limits"].setdefault("stop_reason", error_code)

    summary = str(payload.get("summary") or "")
    if len(summary) > max_chars:
        clipped = summary[: max(0, max_chars - 30)].rstrip() + "\n...[truncated]"
        payload["summary"] = clipped
        if isinstance(payload.get("limits"), dict):
            payload["limits"]["truncated"] = True
    return payload


def dump_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def validate_output(payload: Dict[str, Any], schema: Dict[str, Any] | None) -> List[str]:
    """Check payload against output_schema. Returns list of warning strings.

    This is intentionally lenient (warn-only, never blocks). It checks for
    required keys and basic type matching so BotSpec authors get early feedback
    without risking false-positive rejections.
    """
    if not schema:
        return []
    warnings: List[str] = []
    properties = schema.get("properties", {})
    required_keys = schema.get("required", [])

    for key in required_keys:
        if key not in payload:
            warnings.append(f"output_schema: missing required key '{key}'")

    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    for key, spec in properties.items():
        if key not in payload:
            continue
        expected_type = spec.get("type")
        if expected_type and expected_type in type_map:
            py_type = type_map[expected_type]
            if not isinstance(payload[key], py_type):
                warnings.append(
                    f"output_schema: key '{key}' expected type '{expected_type}', "
                    f"got '{type(payload[key]).__name__}'"
                )
    return warnings


__all__ = [
    "SUBMIT_RESULT_TOOL",
    "SubagentResultHolder",
    "build_result_payload",
    "build_submit_result_tool",
    "dump_payload",
    "validate_output",
]
