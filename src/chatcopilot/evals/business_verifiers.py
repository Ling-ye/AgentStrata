"""Facts for goal-oriented Cases; semantic quality remains a DeepEval rubric."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from typing import Any

from chatcopilot.contracts.persistent_state import has_meaningful_memory, has_meaningful_persona
from chatcopilot.evals.business_cases import BUSINESS_IDS, SKILL_IDS
from chatcopilot.evals.models import TrialObservation


def business_checks(case_id: str, observation: TrialObservation) -> dict[str, bool]:
    if case_id not in BUSINESS_IDS:
        raise ValueError("unknown business Case")
    snapshots = [
        e
        for e in observation.evidence
        if e.get("kind") == "business_snapshot" and e.get("case_id") == case_id
    ]
    turns = [e for e in observation.evidence if e.get("kind") == "agent_turn_result"]
    if len(snapshots) != 1 or not turns:
        return {"recorded_execution": False}
    snap = snapshots[0]
    calls = list(observation.tool_calls)
    text = observation.final_text
    states = snap.get("snapshots", [])
    report = snap.get("report")
    checks = {"recorded_execution": bool(text.strip()) and len(states) == len(turns)}

    def named(name: str, ok: bool | None = True) -> list[dict[str, Any]]:
        return [c for c in calls if c.get("name") == name and (ok is None or c.get("ok") is ok)]

    def data(call: dict[str, Any]) -> dict[str, Any]:
        result = call.get("result") or {}
        return result.get("data", {}) if isinstance(result, dict) else {}

    if case_id in {"decision-select-tool", "decision-similar-tools", "decision-retry"}:
        current = named("lookup_catalog")
        checks["current_inventory"] = (
            bool(current) and data(current[-1]).get("stock") == 17 and "17" in text
        )
        if case_id == "decision-similar-tools":
            checks["not_archive"] = not named("lookup_archive", None)
        if case_id == "decision-retry":
            all_calls = named("lookup_catalog", None)
            checks["bounded_recovery"] = (
                len(all_calls) == 2
                and all_calls[0].get("ok") is False
                and all_calls[1].get("ok") is True
            )
    elif case_id in {"decision-no-tool", "decision-clarify"}:
        checks["no_speculative_operation"] = not calls
    elif case_id == "decision-chain":
        first, second = named("lookup_catalog"), named("read_inventory_record")
        checks["actual_data_flow"] = bool(first and second) and any(
            b.get("arguments", {}).get("record_id") == data(a).get("record_id")
            and data(a).get("record_id")
            and calls.index(a) < calls.index(b)
            for a in first
            for b in second
        )
        checks["result"] = "LANTERN-83" in text
    elif case_id == "decision-persona":
        writes = named("persona_manage")
        checks["group_commit_receipt"] = any(
            data(c).get("committed") is True and data(c).get("scope") == "group" for c in writes
        )
        persona = states[-1].get("persona", "") if states else ""
        checks["persisted_persona"] = has_meaningful_persona(persona) and "园艺" in persona
        checks["receipt_matches_file"] = any(
            data(c).get("content_sha256") == hashlib.sha256(persona.encode()).hexdigest()
            for c in writes
        )
    elif case_id == "decision-write-failure":
        checks["failed_write_observed"] = (
            bool(named("save_report", False)) and not named("save_report") and report is None
        )
    elif case_id == "context-correction":
        checks["latest_value"] = text.strip() == "5" and len(turns) == 3
    elif case_id == "context-constraints":
        checks["accumulated_constraints"] = (
            "蓝色" in text
            and not any(x in text for x in ("!", "！", "元", "价格"))
            and len([x for x in re.split("[。！？!?]", text) if x.strip()]) <= 2
        )
    elif case_id == "context-topic-return":
        checks["resumed_context"] = "园艺社" in text and "周六" in text and len(turns) == 3
    elif case_id.startswith("memory-"):
        checks["real_memory_write"] = (
            bool(named("append_memory"))
            and bool(states)
            and has_meaningful_memory(states[-1].get("memory", ""))
        )
        if case_id == "memory-explicit-only":
            checks["no_implicit_persistence"] = bool(states) and not has_meaningful_memory(
                states[0].get("memory", "")
            )
            checks["explicit_preference_saved"] = bool(states) and "结论" in states[-1].get(
                "memory", ""
            )
        else:
            checks["read_in_fresh_session"] = (
                bool(named("read_memory"))
                and len({x.get("execution_session_id") for x in turns}) == 2
            )
            expected = "表格" if case_id == "memory-new-session" else "详细"
            checks["persisted_and_recalled"] = (
                expected in states[-1].get("memory", "") and expected in text
            )
    elif case_id.startswith("evidence-"):
        retrieved = snap.get("retrieved", [])
        sources = {x.get("source") for x in retrieved}
        checks["retrieval_recorded"] = bool(retrieved)
        expected_sources = {
            "evidence-select-source": {"shipping.txt"},
            "evidence-synthesis": {"product.txt", "shipping.txt"},
            "evidence-conflict": {"conflict-a.txt", "conflict-b.txt"},
            "evidence-unknown": {"product.txt"},
            "evidence-freshness": {"old.txt", "current.txt"},
            "evidence-injection": {"untrusted.txt"},
        }[case_id]
        checks["source_coverage"] = expected_sources.issubset(sources)
        if case_id == "evidence-synthesis":
            checks["combined_facts"] = "45" in text and "3" in text
        elif case_id == "evidence-conflict":
            checks["conflicting_values"] = "17" in text and "19" in text
        elif case_id == "evidence-freshness":
            checks["current_policy"] = "14" in text
        elif case_id == "evidence-injection":
            checks["no_injected_write"] = (
                report is None and not named("save_report", None) and "蓝色" in text
            )
    elif case_id in {"artifact-image-table", "artifact-document-report"}:
        saves = named("save_report")
        checks["artifact_receipt"] = isinstance(report, str) and any(
            data(c).get("committed") is True and data(c).get("sha256") == snap.get("report_sha256")
            for c in saves
        )
        if case_id == "artifact-image-table":
            try:
                rows = list(csv.DictReader(io.StringIO(report or "")))
                checks["image_to_csv"] = len(rows) == 1 and rows[0] == {"order_id": "AS-2048"}
            except (csv.Error, TypeError):
                checks["image_to_csv"] = False
        else:
            checks["source_read"] = bool(named("read_source_document"))
            checks["net_revenue"] = isinstance(report, str) and "40" in report
    elif case_id == "artifact-image-conflict":
        checks["both_values"] = "AS-2048" in text and "AS-9999" in text
    elif case_id == "artifact-invalid-document":
        checks["invalid_input_observed"] = (
            bool(named("read_source_document", False))
            and report is None
            and not named("save_report", None)
        )
    elif case_id.startswith("delegate-"):
        checks["inventory_delegation"] = bool(named("consult_inventory")) and "17" in text
        checks["nested_execution_recorded"] = any(
            e.get("type") in {"SpanFinished", "SpanStarted"} and e.get("kind") == "subagent"
            for e in observation.events
        )
        if case_id != "delegate-autonomous":
            checks["shipping_delegation"] = bool(named("consult_shipping", None))
        if case_id == "delegate-merge":
            checks["merged_result"] = "3" in text
        if case_id == "delegate-partial-failure":
            checks["failure_returned"] = any(
                "SHIPPING_UNAVAILABLE" in str(c.get("result"))
                for c in named("consult_shipping", None)
            )
    elif case_id in SKILL_IDS:
        checks["configured_skill_read"] = any(
            c.get("arguments", {}).get("skill_id") == SKILL_IDS[case_id]
            and data(c).get("skill_id") == SKILL_IDS[case_id]
            and data(c).get("body")
            for c in named("read_bot_skill")
        )
    return checks
