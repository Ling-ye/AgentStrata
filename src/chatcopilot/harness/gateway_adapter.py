"""Read-only task evidence from a host-bound Gateway observation store."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from chatcopilot.core.observability_redaction import redact_observability_payload
from chatcopilot.core.private_sqlite import json_text
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
    blockers = []
    if run["state"] not in {"completed", "failed", "aborted"}:
        blockers.append("机器人任务尚未结束")
    if run.get("details_expired"):
        blockers.append("任务详情已过期，无法建立可靠复现")
    observations = list(record["observations"])
    page = record
    # Bound the evidence package, and expose incomplete captures instead of
    # treating the first page as a complete trace.
    while page["has_more"] and len(observations) < 2000:
        page = events(store, run_id, after=page["next_cursor"], limit=500)
        observations.extend(page["observations"])
    if page["has_more"]:
        blockers.append("任务事件超过单次证据包容量，需缩小复现范围")
    refs = {event["body_ref"] for event in observations if event.get("body_ref")}
    refs.update(run[key] for key in ("input_ref", "result_ref") if run.get(key))
    bodies = {ref: store.body(run_id, ref) for ref in sorted(refs)}
    if not run.get("input_ref") or not bodies.get(run["input_ref"]):
        blockers.append("缺少实际输入记录")
    if any(not body or body.get("state") != "available" for body in bodies.values()):
        blockers.append("任务正文缺失、过期或截断")
    configuration = store.configuration(run["config_id"]) if run.get("config_id") else None
    if not configuration:
        blockers.append("缺少执行时配置快照")
    if not observations:
        blockers.append("缺少执行事件")
    if run.get("capture_state") in {"truncated", "capture_failed"} or any(
        event.get("body_state") in {"truncated", "capture_failed"} for event in observations
    ):
        blockers.append("部分执行详情采集失败或截断")
    evidence = redact_observability_payload(
        {
            "run": run,
            "observations": observations,
            "bodies": bodies,
            "configuration": configuration,
            "receipts": record["receipts"],
            "outbox": record["outbox"],
            "approvals": record["approvals"],
        }
    )
    if evidence.truncated:
        blockers.append("证据包已截断，不能自动判断完整任务行为")
    revision = hashlib.sha256(json_text(evidence.value).encode()).hexdigest()
    return {
        "kind": "robot_task",
        "bot_id": bot_id,
        "run_id": run_id,
        "revision": revision,
        "evidence": evidence.value,
        "blockers": blockers,
        "failure_signature": [{"run_id": run_id, "revision": revision}],
        "case_id": "reproduction",
        "target_id": "local-pytest",
        "case_ids": ["reproduction"],
        "repetitions": 1,
        "passed_cases": [],
    }
