"""Build ToolDef wrappers for subagent delegation."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Callable, Sequence

from chatcopilot.agent.search_policy import validate_search_task_args, validate_write_task_args
from chatcopilot.agent.subagents.runner import SubagentRuntimeConfig, SubagentRunner
from chatcopilot.agent.subagents.search_circuit import SearchCircuitBreaker, _SEARCH_FAILURE_TTLS
from chatcopilot.agent.subagents.spec import SubagentDef
from chatcopilot.agent.subagents.task_pack import TaskPack, parse_task_pack, task_pack_schema
from chatcopilot.agent.trace import current_trace
from chatcopilot.contracts.adapter_approval import (
    AdapterApprovalEnvelope,
    validate_adapter_approval,
)
from chatcopilot.contracts.tools import ToolContext
from chatcopilot.core.adapter_approval import (
    AdapterApprovalStore,
    resolve_adapter_bot_spec,
)
from chatcopilot.external_tools.shared.tool_spec import ToolDef

_CONSECUTIVE_FAILURE_THROTTLE = 2
_KNOWN_WRITE_TOOL_NAMES = frozenset(
    {
        "delete_file",
        "edit_file",
        "finalize_self_update",
        "run_command",
        "start_code_task",
        "write_file",
    }
)


def make_delegate_tool(
    session_id: str,
    definition: SubagentDef,
    runner: SubagentRunner,
    config: SubagentRuntimeConfig,
    allow_tool,
    *,
    availability_hint: str = "",
    date_annotator: Callable[[TaskPack], TaskPack] | None = None,
    module_name: str = __name__,
) -> ToolDef:
    consecutive_failures = [0]
    blocked = [False]
    last_partial: list[str] = []
    failure_trace: list[str | None] = [None]

    def _handler(args: dict, ctx: ToolContext | None = None):
        trace = current_trace()
        trace_id = trace.trace_id if trace is not None else None
        if trace_id is None or trace_id != failure_trace[0]:
            failure_trace[0] = trace_id
            consecutive_failures[0] = 0
            blocked[0] = False
            last_partial.clear()
        if blocked[0]:
            block_msg = (
                f"[BLOCKED] {definition.tool_name} 已连续 {consecutive_failures[0]} 次失败，"
                "本轮禁止再调用。"
            )
            if last_partial[0:1]:
                block_msg += f"\n已有的部分结果：{last_partial[0]}"
            block_msg += "\n请基于已有信息直接回答用户，或告知用户该信息暂不可查。"
            payload = {
                "ok": False,
                "error_code": "delegate_failure_throttled",
                "summary": block_msg,
                "partial_findings": list(last_partial),
                "outputs": [],
                "limits": {"delegate_blocked": True, "scope": "current_turn"},
                "confidence": "low",
            }
            return json.dumps(payload, ensure_ascii=False), [], None

        if definition.kind == "search":
            validation_errors = validate_search_task_args(args)
            if validation_errors:
                payload = {
                    "ok": False,
                    "error_code": "invalid_search_task_pack",
                    "summary": "Invalid search task pack: " + "; ".join(validation_errors),
                    "findings": [],
                    "evidence": [],
                    "outputs": [],
                    "risks": list(validation_errors),
                    "next_steps": [
                        "Retry once with all required structured search fields."
                    ],
                    "confidence": "low",
                    "limits": {"validation_failed": True},
                }
                return json.dumps(payload, ensure_ascii=False), [], None
        elif has_write_selector(definition):
            validation_errors = validate_write_task_args(args)
            if validation_errors:
                payload = {
                    "ok": False,
                    "error_code": "invalid_write_task_pack",
                    "summary": "Invalid write task pack: " + "; ".join(validation_errors),
                    "findings": [],
                    "evidence": [],
                    "outputs": [],
                    "risks": list(validation_errors),
                    "next_steps": [
                        "Retry with objective and write_scope specified."
                    ],
                    "confidence": "low",
                    "limits": {"validation_failed": True},
                }
                return json.dumps(payload, ensure_ascii=False), [], None
            if definition.name == "adapter_forge":
                approval_errors = _validate_adapter_forge_args(args)
                if approval_errors:
                    payload = {
                        "ok": False,
                        "error_code": "invalid_adapter_approval",
                        "summary": "Invalid adapter approval: " + "; ".join(approval_errors),
                        "findings": [],
                        "evidence": [],
                        "outputs": [],
                        "risks": list(approval_errors),
                        "next_steps": [
                            "Retry with the exact immutable Owner-approved source envelope."
                        ],
                        "confidence": "high",
                        "limits": {"validation_failed": True},
                    }
                    return json.dumps(payload, ensure_ascii=False), [], None
                try:
                    _consume_adapter_approval(args, ctx)
                except (FileNotFoundError, PermissionError, RuntimeError, ValueError) as exc:
                    payload = {
                        "ok": False,
                        "error_code": "adapter_approval_required",
                        "summary": str(exc),
                        "findings": [],
                        "evidence": [],
                        "outputs": [],
                        "risks": [str(exc)],
                        "next_steps": [
                            "Prepare the candidate, obtain explicit Owner confirmation, "
                            "record approve_adapter_source, then retry once."
                        ],
                        "confidence": "high",
                        "limits": {"validation_failed": True},
                    }
                    return json.dumps(payload, ensure_ascii=False), [], None

        task_args = dict(args)
        extension_inputs: list[str] = []
        for key in definition.input_schema:
            if key == "_required" or key not in task_args:
                continue
            value = task_args.pop(key)
            extension_inputs.append(
                f"{key}=" + json.dumps(value, ensure_ascii=False, sort_keys=True)
            )
        task = parse_task_pack(task_args)
        if extension_inputs:
            task = replace(task, inputs=(*task.inputs, *extension_inputs))
        if definition.kind == "search" and date_annotator is not None:
            task = date_annotator(task)
        result = runner.run(
            session_id=session_id,
            subagent_name=definition.name,
            task=task,
            role_prompt=definition.role_prompt,
            version=definition.version,
            context_policy=definition.context_policy,
            cache_policy=definition.cache_policy,
            cleanup_tools=definition.cleanup_tools,
            allow_tool=allow_tool,
            config=config,
            unavailable_message=definition.unavailable_message,
            output_schema=definition.output_schema or None,
        )
        if not result.ok:
            consecutive_failures[0] += 1
            if result.summary:
                last_partial.clear()
                last_partial.append(result.summary[:2000])
            if consecutive_failures[0] >= _CONSECUTIVE_FAILURE_THROTTLE:
                blocked[0] = True
                payload = delegate_payload(result.summary)
                if not payload:
                    payload = {"ok": False, "summary": result.summary, "outputs": []}
                payload.setdefault("error_code", result.error_code or "delegate_failure_throttled")
                limits = payload.setdefault("limits", {})
                if isinstance(limits, dict):
                    limits.update({"delegate_blocked": True, "scope": "current_turn"})
                payload["throttle_hint"] = (
                    f"{definition.tool_name} 已连续 {consecutive_failures[0]} 次未完成；"
                    "本轮不要继续重试此来源。"
                )
                return json.dumps(payload, ensure_ascii=False), list(result.outputs), None
        else:
            consecutive_failures[0] = 0
        return result.summary, list(result.outputs), None

    properties = task_pack_schema()
    required = ["objective"]
    if definition.input_schema:
        for key, spec in definition.input_schema.items():
            if key == "_required":
                continue
            properties[key] = {**properties.get(key, {}), **spec}
        schema_required = definition.input_schema.get("_required")
        if isinstance(schema_required, list):
            required = list(dict.fromkeys([*required, *schema_required]))

    return ToolDef(
        name=definition.tool_name,
        summary=summary_with_availability(definition.summary, availability_hint),
        properties=properties,
        required=required,
        handler=_handler,
        category="agent.subagent",
        owner="agent",
        module=module_name,
        artifact_kinds=("file", "directory"),
        requires_role="owner" if definition.name == "adapter_forge" else None,
        metadata={
            "subagent": definition.name,
            "subagent_kind": definition.kind,
            "subagent_version": definition.version,
            "workflow_tags": list(definition.workflow_tags),
            **(
                {"execution_boundary": "codex"}
                if has_write_selector(definition)
                else {}
            ),
        },
    )


def delegate_payload(summary: str) -> dict:
    try:
        payload = json.loads(summary)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def with_web_fallback(
    *,
    primary: ToolDef,
    fallback: ToolDef,
    circuit: SearchCircuitBreaker,
    payload_parser: Callable[[str], dict] = delegate_payload,
) -> ToolDef:
    primary_handler = primary.handler
    fallback_handler = fallback.handler

    def _handler(args: dict):
        primary_block = circuit.blocked("tavily")
        primary_payload: dict = {}
        if primary_block is None:
            primary_summary, primary_outputs, primary_hint = primary_handler(args)
            primary_payload = payload_parser(primary_summary)
            if primary_payload.get("ok") is not False:
                circuit.record_success("tavily")
                return primary_summary, primary_outputs, primary_hint
            primary_block = str(primary_payload.get("error_code") or "") or None
            circuit.record_failure("tavily", primary_block)
            if primary_block not in _SEARCH_FAILURE_TTLS:
                return primary_summary, primary_outputs, primary_hint

        fallback_block = circuit.blocked("searxng")
        if fallback_block is None:
            fallback_summary, fallback_outputs, fallback_hint = fallback_handler(args)
            fallback_payload = payload_parser(fallback_summary)
            if fallback_payload.get("ok") is not False:
                circuit.record_success("searxng")
                fallback_payload["fallback"] = {
                    "from": "tavily",
                    "reason": primary_block,
                    "source": "searxng",
                }
                return json.dumps(fallback_payload, ensure_ascii=False), fallback_outputs, fallback_hint
            fallback_block = str(fallback_payload.get("error_code") or "") or None
            circuit.record_failure("searxng", fallback_block)

        payload = {
            "ok": False,
            "error_code": fallback_block or primary_block or "web_search_unavailable",
            "summary": (
                "Tavily 与 SearXNG 均未能完成联网核实。可以使用模型已有知识兜底，"
                "但回答必须明确标注‘未联网核实’，并提示信息可能过时。"
            ),
            "findings": [],
            "evidence": [],
            "outputs": [],
            "limits": {
                "tavily": primary_block or "failed",
                "searxng": fallback_block or "failed",
                "allow_stale_knowledge": True,
            },
            "confidence": "low",
        }
        return json.dumps(payload, ensure_ascii=False), [], None

    return replace(primary, handler=_handler)


def availability_hint(subagent_name: str, tools: Sequence[ToolDef], allow_tool) -> str:
    mcp_by_owner: dict[str, list[str]] = {}
    for tool in tools:
        if tool.category != "mcp" or not allow_tool(tool):
            continue
        owner = str(tool.metadata.get("mcp_server_id") or tool.owner or "mcp").strip()
        if not owner:
            owner = "mcp"
        mcp_by_owner.setdefault(owner, []).append(tool.name)
    if not mcp_by_owner:
        return ""

    parts: list[str] = []
    for owner in sorted(mcp_by_owner):
        all_names = sorted(dict.fromkeys(mcp_by_owner[owner]))
        total = len(all_names)
        search_names = [n for n in all_names if "search" in n]
        sample = search_names[:3] or all_names[:2]
        label = ", ".join(sample)
        if total > len(sample):
            label += f", ... ({total} tools total)"
        parts.append(f"{owner} [{label}]")
    return (
        f"Available MCP sources for {subagent_name}: "
        + "; ".join(parts)
        + ". These sources have full capabilities including search. "
        "Always delegate to this tool when the user asks for one of these sources; "
        "do not judge availability from the sample tool names above."
    )


def summary_with_availability(summary: str, availability_hint: str) -> str:
    if not availability_hint:
        return summary
    return f"{summary}\n\n{availability_hint}"


def has_write_selector(definition: SubagentDef) -> bool:
    """True when a subagent's selector references write-capable tool categories."""
    for rule in definition.selector.any:
        if any(name in _KNOWN_WRITE_TOOL_NAMES for name in rule.names):
            return True
        if any(name.startswith(("write_", "edit_", "delete_")) for name in rule.names):
            return True
        for category in rule.categories:
            if category.startswith("dev.") or ".write" in category:
                return True
        for prefix in rule.category_prefixes:
            if prefix.startswith("dev.") or ".write" in prefix:
                return True
        if any(str(risk).lower() in {"interactive", "write"} for risk in rule.mcp_risk):
            return True
    return False


def _validate_adapter_forge_args(args: dict) -> tuple[str, ...]:
    """Validate the immutable source envelope before adapter delegation."""

    envelope = _adapter_approval_envelope(args)
    errors = list(validate_adapter_approval(envelope))
    candidate_digest = str(args.get("candidate_digest") or "").strip()
    if (
        not candidate_digest.startswith("sha256:")
        or len(candidate_digest) != 71
        or any(
            char not in "0123456789abcdef"
            for char in candidate_digest.removeprefix("sha256:")
        )
    ):
        errors.append("candidate_digest must be sha256:<64 lowercase hex>")
    elif candidate_digest != envelope.candidate_digest:
        errors.append("candidate_digest does not match the adapter source envelope")
    return tuple(errors)


def _adapter_candidate_digest(args: dict) -> str:
    """Return the digest for the exact adapter approval envelope."""

    return _adapter_approval_envelope(args).candidate_digest


def _adapter_approval_envelope(args: dict) -> AdapterApprovalEnvelope:
    return AdapterApprovalEnvelope(
        resource_name=str(args.get("resource_name") or "").strip(),
        source_url=str(args.get("source_url") or "").strip(),
        approved_ref=str(args.get("approved_ref") or "").strip(),
        license_evidence=str(args.get("license_evidence") or "").strip(),
        integration_intent=str(args.get("integration_intent") or "").strip(),
    )


def _consume_adapter_approval(args: dict, ctx: ToolContext | None) -> None:
    if ctx is None:
        raise PermissionError("adapter delegation requires an authenticated tool context")
    owner_user_id = str(getattr(ctx.workspace, "user_id", "") or "").strip()
    if not owner_user_id:
        raise PermissionError("adapter delegation requires a stable owner user_id")
    bot_path = resolve_adapter_bot_spec(str(args.get("bot") or "").strip() or None)
    AdapterApprovalStore.for_bot(bot_path).consume(
        envelope=_adapter_approval_envelope(args),
        candidate_digest=str(args.get("candidate_digest") or "").strip(),
        consumed_by=owner_user_id,
    )


__all__ = [
    "availability_hint",
    "delegate_payload",
    "has_write_selector",
    "make_delegate_tool",
    "summary_with_availability",
    "with_web_fallback",
]
