"""Read-only task evidence from a host-bound Gateway observation store."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from chatcopilot.core.observability_redaction import redact_observability_payload
from chatcopilot.core.private_sqlite import json_text
from chatcopilot.core.trace_archive import TraceArchive, TraceExpired
from chatcopilot.gateway.observation_queries import detail, events
from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.harness.models import HarnessError


def read_task(root: Path, bot_id: str, run_id: str) -> dict[str, Any]:
    return task_source(ObservationStore(root), bot_id, run_id)


def task_source(store: ObservationStore, bot_id: str, run_id: str) -> dict[str, Any]:
    record = detail(store, run_id)
    if record is None:
        raise HarnessError("not_found", "此实例中没有该机器人任务 ID")
    run = record["run"]
    if run.get("run_id") != run_id:
        raise HarnessError("source_mismatch", "任务观测与请求 ID 不一致")
    input_body = store.body(run_id, run["input_ref"]) if run.get("input_ref") else None
    original_input = (input_body or {}).get("payload") or {}
    original_input = original_input.get("text", "") if isinstance(original_input, dict) else ""
    input_fields = {"original_input": original_input} if isinstance(original_input, str) and original_input else {}
    input_fields["requires_image"] = False
    blockers = []
    warnings: list[dict[str, str]] = []
    bundle = None
    if run["state"] not in {"completed", "failed", "aborted"}:
        blockers.append("机器人任务尚未结束")
    reference = store.meta("trace:" + run_id) or {}
    if reference.get("trace_ref"):
        archive = TraceArchive(store.root / "traces")
        try:
            bundle = archive.export(reference["trace_ref"], source={"kind": "robot_task", "run_id": run_id},
                                    sha256=reference["sha256"])
        except TraceExpired:
            warnings.append({"code": "trace_expired", "message": "本地执行归档正文已过期，使用仍可读取的观测诊断"})
        except FileNotFoundError:
            warnings.append({"code": "trace_missing", "message": "本地执行归档文件缺失，使用仍可读取的观测诊断"})
        if bundle:
            spans = bundle.get("trace", {}).get("baseSpans", [])
            input_fields["requires_image"] = any(_image_input(span.get(field)) for span in spans
                if span.get("name") in {"channel.receive", "application.prepare"} for field in ("input", "output"))
        if bundle and reference.get("capture_state") == "available":
            evidence = redact_observability_payload({"run": run, "trace": reference,
                "configuration": store.configuration(run["config_id"]) if run.get("config_id") else None,
                "receipts": record["receipts"], "outbox": record["outbox"], "approvals": record["approvals"]}).value
            revision = hashlib.sha256(json_text(evidence).encode()).hexdigest()
            return {**input_fields, "kind": "robot_task", "bot_id": bot_id, "run_id": run_id, "revision": revision,
                    "evidence": evidence, "trace_bundle": bundle, "blockers": blockers, "warnings": warnings,
                    "failure_signature": [{"run_id": run_id, "revision": revision}]}
        if bundle:
            warnings.append({"code": "trace_partial", "message": "本地执行归档部分缺失，诊断时需核对验证所需材料"})
    else:
        error = reference.get("error") or {}
        warnings.append({"code": error.get("code") or "trace_unavailable",
                         "message": error.get("message") or "本地执行归档不可用，先用已有任务事件、正文和反馈诊断"})
    if run.get("details_expired"):
        warnings.append({"code": "details_expired", "message": "部分任务详情已过期"})
    observations = list(record["observations"])
    page = record
    # Bound the evidence package, and expose incomplete captures instead of
    # treating the first page as a complete trace.
    while page["has_more"] and len(observations) < 2000:
        page = events(store, run_id, after=page["next_cursor"], limit=500)
        observations.extend(page["observations"])
    if page["has_more"]:
        warnings.append({"code": "events_incomplete", "message": "任务事件超过单次证据包容量，诊断材料不含全部事件"})
    refs = {event["body_ref"] for event in observations if event.get("body_ref")}
    refs.update(run[key] for key in ("input_ref", "result_ref") if run.get(key))
    bodies = {ref: input_body if ref == run.get("input_ref") else store.body(run_id, ref) for ref in sorted(refs)}
    input_fields["requires_image"] = input_fields["requires_image"] or any(
        _image_input((bodies.get(event.get("body_ref")) or {}).get("payload")) for event in observations
        if (event.get("metadata", {}).get("operation") or event.get("name")) in {"channel.receive", "application.prepare"})
    if not run.get("input_ref") or not bodies.get(run["input_ref"]):
        warnings.append({"code": "input_missing", "message": "缺少实际输入记录，需确认能否建立对应原问题的复现"})
    if any(not body or body.get("state") != "available" for body in bodies.values()):
        warnings.append({"code": "bodies_incomplete", "message": "任务正文缺失、过期或截断"})
    configuration = store.configuration(run["config_id"]) if run.get("config_id") else None
    if not configuration:
        warnings.append({"code": "configuration_missing", "message": "缺少执行时配置快照"})
    if not observations:
        warnings.append({"code": "events_missing", "message": "缺少执行事件"})
    if run.get("capture_state") in {"truncated", "capture_failed"} or any(
        event.get("body_state") in {"truncated", "capture_failed"} for event in observations
    ):
        warnings.append({"code": "capture_incomplete", "message": "部分执行详情采集失败或截断"})
    evidence = redact_observability_payload(
        {
            "run": run,
            "trace": reference,
            "observations": observations,
            "bodies": bodies,
            "configuration": configuration,
            "receipts": record["receipts"],
            "outbox": record["outbox"],
            "approvals": record["approvals"],
        }
    )
    if evidence.truncated:
        warnings.append({"code": "evidence_truncated", "message": "观测快照已截断，不能据此声称已覆盖完整任务行为"})
    revision = hashlib.sha256(json_text({"evidence": evidence.value, "warnings": warnings}).encode()).hexdigest()
    return {
        **input_fields,
        "kind": "robot_task",
        "bot_id": bot_id,
        "run_id": run_id,
        "revision": revision,
        "evidence": evidence.value,
        "blockers": blockers,
        "warnings": warnings,
        **({"trace_bundle": bundle} if bundle else {}),
        "failure_signature": [{"run_id": run_id, "revision": revision}],
    }


def _image_input(value: Any) -> bool:
    """Inspect only trusted Channel/Application input summaries, never prose keywords."""
    if not isinstance(value, dict):
        return False
    resources = value.get("resources")
    if isinstance(resources, list) and any(isinstance(ref, dict) and
        (ref.get("kind") == "image" or str(ref.get("media_type", "")).startswith("image/")) for ref in resources):
        return True
    return any(_image_input(value.get(key)) for key in ("input", "output"))
