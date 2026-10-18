"""Failure nodes; control flow is owned by the compiled graph."""

from __future__ import annotations
import time
from dataclasses import asdict
from typing import Any
from langgraph.runtime import Runtime
from chatcopilot.harness.models import Cancelled, HarnessError
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.repair_types import failure_signature
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.engine.context import HarnessContext
from chatcopilot.harness.engine.state import RepairState
from chatcopilot.harness.engine.nodes import node

_STOP_CODES = {
    "budget_exhausted",
    "no_progress",
    "fixture_missing",
    "image_required",
    "protected_change",
    "workspace_changed",
    "index_changed",
    "governance_target_changed",
    "governance_run_stopped",
    "verification_environment",
    "verification_judge",
    "verification_evidence",
    "evaluation_unavailable",
    "evaluation_incomplete",
    "result_pending",
    "coding_environment",
    "model_unavailable",
    "review_inconclusive",
    "plan_blocked",
    "material_missing",
    "session_unconfirmed",
    "session_changed",
    "operation_changed",
    "source_mismatch",
    "source_changed",
    "incomplete_verification",
    "verification_purpose",
    "execution_error",
}


def _passed_checks(attempt: dict[str, Any]) -> set[str]:
    """Count host-observed check progress even before a whole goal passes."""
    return {
        f"{phase}:{check}"
        for phase in ("verification", "confirmation", "repository_regressions")
        for check in (attempt.get(phase, {}).get("passed_cases") or ())
    } | {f"definition:{check}" for check in attempt.get("valid_definition_checks", ())}


@node
def failure(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    details = ArtifactRepository(ctx.store.root / "jobs" / ctx.task_id).read(state["error_ref"])
    code, stage = (details["code"], details["phase"])
    exc = HarnessError(code, details["message"])
    current = ctx.store.get(ctx.task_id)
    if (
        current["status"] == "cancel_requested"
        and current.get("current_evaluation_id")
        and code in {"evaluation_unavailable", "result_pending"}
    ):
        ctx.finish("cancel_requested", code, details["message"])
        return {"outcome": "cancel_requested"}
    if details.get("evidence"):
        exc.evidence = details["evidence"]
    if details.get("cancelled"):
        if state.get("attempt"):
            attempt = ctx.attempt(state)
            try:
                captured = ctx.artifacts.capture(state["attempt"], current["baseline_manifest"])
            except (HarnessError, OSError, ValueError) as capture_error:
                attempt["capture_error"] = safe_error(capture_error)
            else:
                attempt.update(captured)
                ctx.store.update(
                    ctx.task_id,
                    working_digest=attempt["candidate_digest"],
                    candidate_checkpoint={
                        key: attempt[key]
                        for key in (
                            "number",
                            "candidate_digest",
                            "patch_path",
                            "patch_sha256",
                            "changed_files",
                        )
                    },
                )
            attempt.update(
                status="interrupted",
                error_code="cancelled",
                error=details["message"],
                finished_at=time.time(),
            )
            ctx.save(state, attempt)
        ctx.finish("cancelled", "cancelled", "修复已取消，候选与证据已保留")
        return {"outcome": "cancelled"}
    if not state.get("attempt"):
        ctx.finish("blocked", code, details["message"])
        return {"outcome": "blocked"}
    number = state["attempt"]
    attempt = ctx.attempt(state)
    previous_row = next(
        (row for row in reversed(ctx.store.attempts(ctx.task_id)) if row["number"] < number),
        None,
    )
    previous = (previous_row or {}).get("feedback")
    previous_checks = _passed_checks(previous_row) if previous_row else set()
    governance = ctx.governance
    evaluations = ctx.store.get(ctx.task_id).get("evaluations", {})
    for phase, field in (
        (f"reproduce-r{number}", "reproduction"),
        (f"verify-{number}", "verification"),
        (f"confirm-{number}", "confirmation"),
    ):
        if evaluations.get(phase, {}).get("complete"):
            attempt[field] = evaluations[phase]
    code = exc.code
    if code in {"test_definition", "invalid_submission", "verification_test_definition"}:
        stage = "definition"
    elif code in {"needs_replan", "invalid_role_result"}:
        stage = "plan"
    try:
        if "candidate_digest" not in attempt:
            attempt.update(
                ctx.artifacts.capture(number, ctx.store.get(ctx.task_id)["baseline_manifest"])
            )
        ctx.store.update(
            ctx.task_id,
            working_digest=attempt["candidate_digest"],
            candidate_checkpoint={
                k: attempt[k]
                for k in (
                    "number",
                    "candidate_digest",
                    "patch_path",
                    "patch_sha256",
                    "changed_files",
                )
            },
        )
    except (HarnessError, OSError, ValueError):
        if code not in {"workspace_changed", "protected_change", "index_changed"}:
            raise
    keys = sorted(
        (k for k, row in attempt.get("acceptance_coverage", {}).items() if not row["passed"])
    ) or [stage]
    signature = failure_signature(stage, code, keys)
    passed = sorted(
        (k for k, row in attempt.get("acceptance_coverage", {}).items() if row["passed"])
    )
    if stage == "definition":
        rows = (getattr(exc, "evidence", None) or {}).get("result", {}).get("rows", {})
        attempt["valid_definition_checks"] = sorted(
            (
                name.split("::", 1)[-1]
                for name, row in rows.items()
                if ctx.scenario.classify_check(row) in {"", "product"}
            )
        )
    current_checks = _passed_checks(attempt)
    repeated = (previous or {}).get("signature") == signature and (
        not current_checks - previous_checks
    )
    previous_checks = current_checks
    feedback = {
        "stage": stage,
        "code": code,
        "message": safe_error(exc),
        "requirements": keys,
        "passed_requirements": passed,
        "signature": signature,
    }
    used_rediagnosis = any(
        (row.get("feedback", {}).get("rediagnosis") for row in ctx.store.attempts(ctx.task_id))
    )
    if (
        repeated
        and code in {"product_failure", "test_definition", "verification_test_definition"}
        and (attempt.get("verification") or getattr(exc, "evidence", None))
        and (not governance)
        and (not used_rediagnosis)
        and (
            sum((row.get("counts_toward_budget", True) for row in ctx.store.attempts(ctx.task_id)))
            < ctx.options.max_attempts
        )
    ):
        ctx.cancel()
        feedback.update(
            rediagnosis=True,
            next_role="plan",
            retry_reason="连续两轮无新增通过项，使用剩余预算进行唯一一次重诊断",
        )
    if (
        code in _STOP_CODES
        or isinstance(exc, Cancelled)
        or (repeated and (not feedback.get("rediagnosis")))
    ):
        feedback.update(
            next_role="stop",
            retry_reason="连续两轮没有新增有效验收证据"
            if repeated and code not in _STOP_CODES
            else safe_error(exc),
        )
    attempt.update(
        status="interrupted" if code == "session_unconfirmed" else "rejected",
        error=safe_error(exc),
        error_code=code,
        finished_at=time.time(),
        feedback=feedback,
        counts_toward_budget=attempt.get("counts_toward_budget", True)
        if code in _STOP_CODES
        else True,
    )
    evidence_ref = None
    if getattr(exc, "evidence", None):
        ref = ctx.records.put("verification_error", number, exc.evidence)
        evidence_ref = asdict(ref)
    brief = ctx.scenario.failure_brief(
        attempt=attempt,
        stage=stage,
        code=code,
        message=safe_error(exc),
        signature=signature,
        evidence_ref=evidence_ref,
        evidence=getattr(exc, "evidence", None),
        previous=next(
            (row for row in reversed(ctx.store.attempts(ctx.task_id)) if row["number"] < number),
            None,
        ),
    )
    brief_ref = ctx.records.put("failure_brief", number, brief)
    attempt["failure_brief"] = asdict(brief_ref)
    attempt["feedback"] = {**feedback, "brief_ref": asdict(brief_ref)}
    ctx.store.update(ctx.task_id, failure_brief_ref=asdict(brief_ref))
    ctx.store.save_attempt(ctx.task_id, number, attempt)
    previous = attempt["feedback"]
    if code in _STOP_CODES or (repeated and (not feedback.get("rediagnosis"))):
        final_code = "no_progress" if repeated and code not in _STOP_CODES else code
        ctx.finish(
            "blocked",
            final_code,
            "连续两轮没有新增有效验收证据" if final_code == "no_progress" else details["message"],
        )
        return {"outcome": "blocked"}
    return {
        "error_ref": None,
        "deferred_error_ref": None,
        "replaying": False,
        "route": "begin_attempt",
        "failure_ref": asdict(brief_ref),
    }
