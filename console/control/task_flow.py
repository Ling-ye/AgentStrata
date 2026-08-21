"""Stable Console projection for one task's cross-layer execution flow."""
from __future__ import annotations

from collections import Counter
import hashlib
from typing import Dict, Iterable, List, Mapping


FLOW_SCHEMA_VERSION = 1
MAX_FLOW_TRANSITIONS = 300
MAX_FLOW_TEXT = 320

LAYER_ORDER = (
    "channel",
    "transport",
    "gateway",
    "middleware",
    "agent",
    "model",
    "capability",
    "delivery",
)

_LAYER_LABELS = {
    "channel": "外部渠道",
    "transport": "NapCat / OneBot / cc-connect",
    "gateway": "接入网关",
    "middleware": "ACP 中间件",
    "agent": "主 Agent",
    "model": "模型",
    "capability": "工具 / 子 Agent / 流程",
    "delivery": "回复交付",
}

_EVIDENCE_LEVELS = {
    "observed",
    "correlated",
    "declared",
    "provider_opaque",
    "missing",
}
_STATUSES = {"pending", "running", "succeeded", "failed", "skipped", "unknown"}


def project_task_flow(
    *,
    instance_id: str,
    task: Mapping[str, object],
    events: Iterable[Mapping[str, object]],
    events_truncated: bool = False,
    integrity_gap: bool = False,
) -> Dict[str, object]:
    """Normalize private runtime events into the frontend's bounded flow contract."""

    task_id = str(task.get("task_id") or "")
    transitions: List[Dict[str, object]] = []
    for event in events:
        projected = _project_event(event, len(transitions) + 1)
        if projected is not None:
            transitions.append(projected)

    projection_truncated = len(transitions) > MAX_FLOW_TRANSITIONS
    if projection_truncated:
        transitions = transitions[-MAX_FLOW_TRANSITIONS:]

    coverage_counts = Counter(
        str(item.get("evidence_level") or "missing") for item in transitions
    )
    layer_coverage = _layer_coverage(transitions)
    omissions = _omissions(
        layer_coverage,
        transitions,
        task,
        events_truncated,
        integrity_gap,
    )

    return {
        "schema_version": FLOW_SCHEMA_VERSION,
        "instance_id": instance_id,
        "task_id": task_id,
        "status": _status(task.get("status")),
        "layers": [
            {
                "id": layer_id,
                "label": _LAYER_LABELS[layer_id],
                "coverage": layer_coverage[layer_id],
                "status": _layer_status(layer_id, transitions),
                "transition_count": sum(
                    1
                    for transition in transitions
                    if layer_id
                    in {
                        transition.get("source_layer"),
                        transition.get("target_layer"),
                    }
                ),
            }
            for layer_id in LAYER_ORDER
        ],
        "transitions": transitions,
        "coverage": {
            "observed": coverage_counts["observed"],
            "correlated": coverage_counts["correlated"],
            "declared": coverage_counts["declared"],
            "provider_opaque": coverage_counts["provider_opaque"],
            "missing": sum(
                1 for value in layer_coverage.values() if value == "missing"
            ),
            "events_truncated": bool(events_truncated or projection_truncated),
            "integrity_gap": bool(integrity_gap),
        },
        "omissions": omissions,
        "delivery_claim": _delivery_claim(transitions),
    }


def _project_event(
    event: Mapping[str, object],
    fallback_sequence: int,
) -> Dict[str, object] | None:
    event_type = str(event.get("event") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    sequence_value = event.get("sequence")
    sequence = (
        sequence_value
        if isinstance(sequence_value, int)
        and not isinstance(sequence_value, bool)
        and sequence_value >= 0
        else fallback_sequence
    )
    occurred_at = _number(event.get("recorded_at"))
    evidence = {
        "source": "job" if event.get("source") == "job" else "task",
        "event_type": event_type,
        "event_id": _clip(event.get("event_id"), 160),
        "sequence": sequence,
    }
    job_id = _clip(event.get("job_id"), 160)
    if job_id:
        evidence["job_id"] = job_id

    if event_type == "flow_transition":
        source = str(data.get("source_layer") or "")
        target = str(data.get("target_layer") or "")
        if source not in LAYER_ORDER or target not in LAYER_ORDER:
            return None
        level = str(data.get("evidence_level") or "observed")
        if level not in _EVIDENCE_LEVELS:
            level = "observed"
        return _transition(
            sequence=sequence,
            kind=_clip(data.get("kind"), 120) or "runtime.flow",
            source=source,
            target=target,
            status=_status(data.get("status")),
            evidence_level=level,
            title=_clip(data.get("title"), 120) or "运行时阶段",
            summary=_clip(data.get("summary")),
            occurred_at=occurred_at,
            duration_ms=_number(data.get("duration_ms")),
            decision=_safe_decision(data.get("decision")),
            payload=_safe_payload(data.get("payload")),
            evidence=evidence,
        )

    if event_type == "task_started":
        return _transition(
            sequence=sequence,
            kind="middleware.task_intake",
            source="middleware",
            target="middleware",
            status="succeeded",
            evidence_level="observed",
            title="ACP 已创建任务记录",
            summary="上游传输细节需独立接入证据，任务创建本身不证明网关决策。",
            occurred_at=occurred_at,
            evidence=evidence,
        )

    if event_type == "context_snapshot":
        coverage = str(data.get("coverage") or "provider_opaque")
        evidence["snapshot_id"] = _clip(data.get("snapshot_id"), 160)
        return _transition(
            sequence=sequence,
            kind="model.context_prepared",
            source="agent",
            target="model",
            status=(
                "succeeded"
                if str(data.get("capture_status") or "captured") == "captured"
                else "failed"
            ),
            evidence_level=(
                "provider_opaque" if coverage == "provider_opaque" else "observed"
            ),
            title="模型上下文已准备",
            summary=_context_summary(data),
            occurred_at=occurred_at,
            payload={
                "snapshot_id": _clip(data.get("snapshot_id"), 160),
                "backend": _clip(data.get("backend"), 80),
                "model": _clip(data.get("model"), 120),
                "coverage": _clip(coverage, 80),
                "capture_status": _clip(data.get("capture_status"), 80),
                "message_count": _safe_count(data.get("message_count")),
                "tool_schema_count": _safe_count(data.get("tool_schema_count")),
                "resource_count": _safe_count(data.get("resource_count")),
                "omitted": _safe_string_list(data.get("omitted"), 16),
            },
            evidence=evidence,
        )

    if event_type in {"llm_call_started", "llm_call_finished", "llm_call_failed"}:
        finished = event_type != "llm_call_started"
        ok = bool(data.get("ok", event_type != "llm_call_failed"))
        return _transition(
            sequence=sequence,
            kind=("model.response" if finished else "model.request"),
            source=("model" if finished else "agent"),
            target=("agent" if finished else "model"),
            status=("succeeded" if ok else "failed"),
            evidence_level="observed",
            title=("模型调用完成" if finished else "模型调用开始"),
            summary=_model_summary(data, finished=finished),
            occurred_at=occurred_at,
            payload={
                "backend": _clip(data.get("backend"), 80),
                "model": _clip(data.get("model") or data.get("name"), 120),
                "iteration": _safe_count(data.get("iteration")),
                "role": _clip(data.get("role"), 40),
                "context_snapshot_id": _clip(
                    data.get("context_snapshot_id"), 160
                ),
                "finish_reason": _clip(data.get("finish_reason"), 120),
            },
            evidence=evidence,
        )

    if event_type in {"tool_started", "tool_finished"}:
        finished = event_type == "tool_finished"
        ok = str(data.get("status") or "") != "failed"
        name = _clip(data.get("name"), 120) or "未命名工具"
        return _transition(
            sequence=sequence,
            kind=("capability.result" if finished else "capability.invoke"),
            source=("capability" if finished else "agent"),
            target=("agent" if finished else "capability"),
            status=("succeeded" if ok else "failed"),
            evidence_level="observed",
            title=(f"工具返回：{name}" if finished else f"调用工具：{name}"),
            summary=(
                _clip(data.get("summary") or data.get("error"))
                if finished
                else "参数与结果按任务观测脱敏策略按需展开。"
            ),
            occurred_at=occurred_at,
            duration_ms=_seconds_to_ms(data.get("elapsed_s")),
            payload={
                "name": name,
                "kind": "tool",
                "depth": _safe_count(data.get("depth")),
                "span_id": _clip(data.get("span_id"), 160),
            },
            evidence=evidence,
        )

    if event_type in {"span_started", "span_finished"}:
        finished = event_type == "span_finished"
        kind = _clip(data.get("kind"), 80) or "activity"
        name = _clip(data.get("name"), 120) or kind
        ok = bool(data.get("ok", True))
        return _transition(
            sequence=sequence,
            kind=("capability.result" if finished else "capability.invoke"),
            source=("capability" if finished else "agent"),
            target=("agent" if finished else "capability"),
            status=("succeeded" if ok else "failed"),
            evidence_level=(
                "provider_opaque"
                if kind == "provider_activity_omitted"
                else "observed"
            ),
            title=(f"{_kind_label(kind)}返回：{name}" if finished else f"{_kind_label(kind)}：{name}"),
            summary=_clip(data.get("summary")),
            occurred_at=occurred_at,
            duration_ms=_seconds_to_ms(data.get("elapsed_s")),
            payload={
                "name": name,
                "kind": kind,
                "depth": _safe_count(data.get("depth")),
                "span_id": _clip(data.get("span_id"), 160),
            },
            evidence=evidence,
        )

    if event_type == "input_resources_dispatched":
        return _transition(
            sequence=sequence,
            kind="agent.resources_dispatched",
            source="middleware",
            target="agent",
            status="succeeded",
            evidence_level="observed",
            title="输入资源已交给 Agent",
            summary=f"{_safe_count(data.get('resource_count'))} 个受控资源。",
            occurred_at=occurred_at,
            payload={
                "backend": _clip(data.get("backend"), 80),
                "resource_count": _safe_count(data.get("resource_count")),
            },
            evidence=evidence,
        )

    if event_type in {"topic_decision", "persona_decision", "persona_outcome", "persona_draft"}:
        title = {
            "topic_decision": "上下文路由决策",
            "persona_decision": "人格意图决策",
            "persona_outcome": "人格控制结果",
            "persona_draft": "人格草案流程",
        }[event_type]
        failed = bool(data.get("error_code")) or data.get("ok") is False
        return _transition(
            sequence=sequence,
            kind=f"middleware.{event_type}",
            source="middleware",
            target="middleware",
            status="failed" if failed else "succeeded",
            evidence_level="observed",
            title=title,
            summary=_clip(data.get("reason") or data.get("outcome") or data.get("error_code")),
            occurred_at=occurred_at,
            decision=_safe_decision(data),
            evidence=evidence,
        )

    if event_type == "job_submitted":
        return _transition(
            sequence=sequence,
            kind="capability.background_job",
            source="agent",
            target="capability",
            status="running",
            evidence_level="observed",
            title="后台任务已提交",
            summary="后台任务继续独立执行，最终状态以其受控记录为准。",
            occurred_at=occurred_at,
            payload={"job_id": _clip(data.get("job_id"), 160)},
            evidence=evidence,
        )

    if event_type == "job_stage_changed":
        failed = str(data.get("status") or "") == "failed"
        return _transition(
            sequence=sequence,
            kind="capability.background_job_stage",
            source="capability",
            target="capability",
            status="failed" if failed else _status(data.get("status")),
            evidence_level="observed",
            title=f"后台阶段：{_clip(data.get('stage'), 120) or 'unknown'}",
            summary=_clip(data.get("message") or data.get("error_code")),
            occurred_at=occurred_at,
            payload={
                "job_id": job_id,
                "stage": _clip(data.get("stage"), 120),
                "error_code": _clip(data.get("error_code"), 120),
            },
            evidence=evidence,
        )

    if event_type in {"task_finished", "task_delegated"}:
        failed = str(data.get("status") or "") == "failed" or bool(data.get("error"))
        return _transition(
            sequence=sequence,
            kind="delivery.agent_result",
            source="agent",
            target="delivery",
            status="failed" if failed else "succeeded",
            evidence_level="observed",
            title="Agent 已形成最终结果",
            summary="该证据不等于 QQ 客户端已显示或用户已读取。",
            occurred_at=occurred_at,
            decision={
                "code": _clip(data.get("stop_reason"), 120),
                "outcome": _clip(data.get("status"), 80),
            },
            payload={
                "final_text_present": bool(data.get("final_text")),
                "final_text_delivered": bool(data.get("final_text_delivered")),
            },
            evidence=evidence,
        )

    if event_type == "turn_error":
        error_code = _clip(data.get("code"), 120) or "agent_turn_failed"
        return _transition(
            sequence=sequence,
            kind="delivery.turn_error",
            source="agent",
            target="delivery",
            status="failed",
            evidence_level="observed",
            title="Agent 回合失败",
            summary=f"错误代码：{error_code}",
            occurred_at=occurred_at,
            decision={"code": error_code},
            evidence=evidence,
        )

    if event_type == "provider_activity_omitted":
        return _transition(
            sequence=sequence,
            kind="capability.provider_activity_omitted",
            source="capability",
            target="capability",
            status="skipped",
            evidence_level="provider_opaque",
            title="部分 provider 活动未保留",
            summary="已达到有界观测上限；未保留的内部活动不会被推断。",
            occurred_at=occurred_at,
            payload={
                "reason": _clip(data.get("reason"), 120),
                "retained_summary_limit": _safe_count(data.get("retained_summary_limit")),
            },
            evidence=evidence,
        )
    return None


def _transition(
    *,
    sequence: int,
    kind: str,
    source: str,
    target: str,
    status: str,
    evidence_level: str,
    title: str,
    summary: str = "",
    occurred_at: float | None = None,
    duration_ms: float | None = None,
    decision: Mapping[str, object] | None = None,
    payload: Mapping[str, object] | None = None,
    evidence: Mapping[str, object],
) -> Dict[str, object]:
    evidence_identity = "|".join(
        (
            str(evidence.get("source") or ""),
            str(evidence.get("event_id") or ""),
            str(evidence.get("job_id") or ""),
            str(sequence),
            kind,
        )
    )
    identity_digest = hashlib.sha256(evidence_identity.encode("utf-8")).hexdigest()[:12]
    return {
        "id": f"flow_{sequence:06d}_{kind.replace('.', '_')[:40]}_{identity_digest}",
        "sequence": sequence,
        "kind": kind,
        "source_layer": source,
        "target_layer": target,
        "status": status if status in _STATUSES else "unknown",
        "evidence_level": evidence_level,
        "title": title,
        "summary": summary,
        "occurred_at": occurred_at,
        "duration_ms": duration_ms,
        "decision": dict(decision or {}),
        "payload": dict(payload or {}),
        "evidence": [dict(evidence)],
    }


def _layer_coverage(transitions: Iterable[Mapping[str, object]]) -> Dict[str, str]:
    levels: Dict[str, set[str]] = {layer_id: set() for layer_id in LAYER_ORDER}
    for item in transitions:
        level = str(item.get("evidence_level") or "missing")
        for layer_id in {item.get("source_layer"), item.get("target_layer")}:
            if isinstance(layer_id, str) and layer_id in levels:
                levels[layer_id].add(level)
    priority = ("observed", "correlated", "provider_opaque", "declared")
    return {
        layer_id: next(
            (candidate for candidate in priority if candidate in observed),
            "missing",
        )
        for layer_id, observed in levels.items()
    }


def _layer_status(layer_id: str, transitions: Iterable[Mapping[str, object]]) -> str:
    statuses = [
        str(item.get("status") or "unknown")
        for item in transitions
        if layer_id in {item.get("source_layer"), item.get("target_layer")}
    ]
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    if "succeeded" in statuses:
        return "succeeded"
    if "skipped" in statuses:
        return "skipped"
    return "unknown"


def _omissions(
    layer_coverage: Mapping[str, str],
    transitions: Iterable[Mapping[str, object]],
    task: Mapping[str, object],
    events_truncated: bool,
    integrity_gap: bool,
) -> List[Dict[str, str]]:
    transition_list = list(transitions)
    omissions: List[Dict[str, str]] = []
    messages = {
        "channel": "没有外部客户端回执；不能证明用户已收到或阅读。",
        "transport": "没有可绑定到该任务的 NapCat / OneBot / cc-connect 传输证据。",
        "gateway": "没有可绑定到该任务的接入网关决策收据。",
        "model": "没有模型上下文或调用事件；模型内部状态不会被推断。",
        "capability": "本任务没有观察到工具、子 Agent 或流程调用。",
        "delivery": "没有观察到 Agent 最终结果或后续交付边界。",
    }
    for layer_id in LAYER_ORDER:
        if layer_coverage[layer_id] == "missing" and layer_id in messages:
            omissions.append(
                {"code": f"{layer_id}_evidence_missing", "layer": layer_id, "message": messages[layer_id]}
            )
    gateway_receipts = [
        item
        for item in transition_list
        if item.get("kind") == "gateway.access_decision"
    ]
    if not gateway_receipts or all(
        item.get("evidence_level") == "missing" for item in gateway_receipts
    ):
        omissions.append(
            {
                "code": "gateway_ingress_receipt_missing",
                "layer": "gateway",
                "message": "没有可精确关联的 QQ 接入代理准入收据；ACP 授权证据仍独立有效。",
            }
        )
    snapshots = task.get("context_snapshots")
    if isinstance(snapshots, list) and any(
        isinstance(item, dict) and item.get("coverage") == "provider_opaque"
        for item in snapshots
    ):
        omissions.append(
            {
                "code": "provider_state_opaque",
                "layer": "model",
                "message": "Provider 原生续接状态或内部 instructions 对 AgentStrata 不可见。",
            }
        )
    if events_truncated:
        omissions.append(
            {
                "code": "event_window_truncated",
                "layer": "middleware",
                "message": "原始事件窗口已截断，当前链路可能不完整。",
            }
        )
    if integrity_gap:
        omissions.append(
            {
                "code": "event_integrity_gap",
                "layer": "middleware",
                "message": "事件序列存在完整性缺口，缺失部分不会被重建。",
            }
        )
    return omissions


def _delivery_claim(transitions: Iterable[Mapping[str, object]]) -> Dict[str, object]:
    kinds = {
        str(item.get("kind") or "")
        for item in transitions
        if item.get("status") == "succeeded"
    }
    if "delivery.transport_acknowledged" in kinds:
        boundary = "transport_acknowledged"
        message = "传输 hook 已确认出站边界；仍不证明 QQ 客户端已显示或用户已阅读。"
    elif "delivery.session_update" in kinds:
        boundary = "acp_session_update"
        message = "ACP 已发出 session_update；未观察到外部客户端回执。"
    elif "delivery.agent_result" in kinds:
        boundary = "agent_result"
        message = "仅证明 Agent 形成了结果；外部送达未验证。"
    else:
        boundary = "unverified"
        message = "没有可用的出站交付证据。"
    return {
        "boundary": boundary,
        "qq_client_displayed": False,
        "user_read": False,
        "message": message,
    }


def _context_summary(data: Mapping[str, object]) -> str:
    coverage = _clip(data.get("coverage"), 80) or "provider_opaque"
    if coverage == "exact_model_input":
        return "已捕获实际提交的文本模型输入；内容需通过受控快照按需查看。"
    if coverage == "adapter_visible":
        return "已捕获 AgentStrata 可见的适配器输入；Provider 原生状态仍不可见。"
    return "仅有不透明边界收据；不会推断 Provider 内部上下文或思维过程。"


def _model_summary(data: Mapping[str, object], *, finished: bool) -> str:
    model = _clip(data.get("model") or data.get("name"), 120) or "未标识模型"
    role = _clip(data.get("role"), 40)
    if finished:
        reason = _clip(data.get("finish_reason"), 120)
        return f"{model}{f' · {role}' if role else ''}{f' · {reason}' if reason else ''}"
    return f"{model}{f' · {role}' if role else ''}；隐藏思维不会进入任务记录。"


def _kind_label(kind: str) -> str:
    return {
        "subagent": "子 Agent",
        "workflow": "流程",
        "web_search": "搜索",
        "mcp_tool": "MCP 能力",
        "command": "命令",
        "file_change": "文件变更",
        "provider_event": "Provider 活动",
    }.get(kind, "能力活动")


def _safe_decision(value: object) -> Dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    allowed = ("code", "outcome", "reason", "allowed", "authoritative", "policy")
    result: Dict[str, object] = {}
    for key in allowed:
        raw = value.get(key)
        if isinstance(raw, bool):
            result[key] = raw
        elif raw is not None:
            result[key] = _clip(raw, 160)
    return result


def _safe_payload(value: object) -> Dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    allowed = (
        "adapter",
        "backend",
        "chat_kind",
        "correlation",
        "message_kind",
        "model",
        "resource_count",
        "stage",
        "text_length",
        "tool_count",
    )
    result: Dict[str, object] = {}
    for key in allowed:
        raw = value.get(key)
        if isinstance(raw, bool):
            result[key] = raw
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            result[key] = raw
        elif raw is not None:
            result[key] = _clip(raw, 160)
    return result


def _safe_string_list(value: object, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    return [_clip(item, 160) for item in value[:limit] if _clip(item, 160)]


def _status(value: object) -> str:
    normalized = str(value or "unknown").lower()
    aliases = {
        "done": "succeeded",
        "success": "succeeded",
        "completed": "succeeded",
        "cancelled": "skipped",
        "delegated": "running",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _STATUSES else "unknown"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 and number < float("inf") else None


def _seconds_to_ms(value: object) -> float | None:
    seconds = _number(value)
    return round(seconds * 1000, 3) if seconds is not None else None


def _safe_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return count if 0 <= count <= (1 << 63) - 1 else 0


def _clip(value: object, limit: int = MAX_FLOW_TEXT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["FLOW_SCHEMA_VERSION", "LAYER_ORDER", "project_task_flow"]
