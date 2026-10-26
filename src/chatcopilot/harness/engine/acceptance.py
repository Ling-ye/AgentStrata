"""Acceptance nodes; control flow is owned by the compiled graph."""

from __future__ import annotations
import hashlib
import time
from dataclasses import asdict
from langgraph.runtime import Runtime
from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.models import (
    acceptance_digest,
    VerificationRequest,
    Cancelled,
    HarnessError,
    review_decision,
)
from chatcopilot.harness.agent_types import AcceptedCandidate
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.engine.context import HarnessContext
from chatcopilot.harness.engine.state import RepairState
from chatcopilot.harness.engine.nodes import node
from chatcopilot.harness.engine.evaluation import evaluate


@node
def definition(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    if state.get("replaying"):
        return {}
    current = ctx.store.get(ctx.task_id)
    attempt = ctx.attempt(state)
    proposal = ctx.records.read(state["submission_ref"])
    source, capabilities, requirements = ctx.scenario.definition_source(current, proposal)
    ctx.store.update(
        ctx.task_id,
        source=source,
        acceptance=requirements,
        goal_capabilities=capabilities,
        stage="definition",
    )
    source, plan = ctx.verifier.prepare(
        VerificationRequest.from_task(ctx.store.get(ctx.task_id)),
        ctx.baseline_candidate,
        ctx.artifacts.attempt_directory(state["attempt"]),
        proposal,
        ctx.cancel,
    )
    source = ctx.scenario.retain_conditions(source, ctx.context(state)["prior_source"])
    ctx.require_purpose(plan)
    ctx.store.update(
        ctx.task_id,
        source=source,
        verification_plan=plan.to_payload(),
        plan_generation=state["attempt"],
        reproduction_phase=f"reproduce-r{state['attempt']}",
    )
    attempt["verification_plan"] = plan.to_payload()
    ctx.save(state, attempt)
    return {
        "goal_ref": ctx.put("goal", state["attempt"], ctx.store.get(ctx.task_id)["acceptance"]),
        "plan_ref": ctx.put("verification_plan", state["attempt"], plan.to_payload()),
    }


@node
def baseline(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    task, attempt, plan, candidate = ctx.inputs(state)
    ctx.require_purpose(plan)
    reproduction = evaluate(
        ctx, ctx.baseline_candidate, plan, f"reproduce-r{state['attempt']}", list(plan.checks)
    )
    attempt["reproduction"] = reproduction
    ctx.save(state, attempt)
    if not attempt["changed_files"] and set(plan.primary_checks).issubset(
        reproduction["passed_cases"]
    ):
        if ctx.governance:
            raise HarnessError("needs_replan", "治理计划未产生候选变化，不能声明目标已完成")
        attempt["status"] = "not_reproduced"
        ctx.save(state, attempt)
        ctx.finish("not_reproduced", message="冻结基线已满足目标检查，未生成修复")
        return {"outcome": "not_reproduced"}
    if not attempt["changed_files"]:
        raise HarnessError("empty_candidate", "原问题仍存在，但未生成产品修改")
    protected = sorted(
        set(plan.protected_checks) | set(reproduction["passed_cases"]) - set(plan.primary_checks)
    )
    ctx.store.update(ctx.task_id, protected_cases=protected)


@node
def verification(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    task, attempt, plan, candidate = ctx.inputs(state)
    number = state["attempt"]
    proposal = attempt["submission"]
    protected = task["protected_cases"]
    verification = evaluate(ctx, candidate, plan, f"verify-{number}", list(plan.checks))
    attempt["verification"] = verification
    missing = sorted(
        (set(plan.primary_checks) | set(protected)) - set(verification["passed_cases"])
    )
    coverage = {
        key: {
            "checks": list(checks),
            "passed": bool(checks) and set(checks).issubset(verification["passed_cases"]),
        }
        for key, checks in plan.coverage.items()
    }
    gaps = [*proposal["gaps"], *plan.gaps]
    gaps.extend(
        (
            {"requirement": key, "code": "unverified", "message": "必需目标缺少通过证据"}
            for key, item in coverage.items()
            if not item["passed"]
        )
    )
    attempt.update(acceptance_coverage=coverage, gaps=gaps)
    ctx.store.update(ctx.task_id, acceptance_coverage=coverage, verification_gaps=gaps)
    attempt["regressions"] = sorted(set(protected) - set(verification["passed_cases"]))
    ctx.save(state, attempt)
    if missing:
        raise HarnessError("product_failure", "候选目标或保护检查未通过")
    return {
        "verification_ref": ctx.put(
            "verification", state["attempt"], ctx.attempt(state).get("verification")
        )
    }


@node
def regressions(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    task, attempt, plan, candidate = ctx.inputs(state)
    ctx.store.update(ctx.task_id, stage="regressions")
    library = ctx.store.get(ctx.task_id).get("regression_baseline")
    if library is None:
        library = ctx.verifier.regressions(
            VerificationRequest.from_task(ctx.store.get(ctx.task_id)),
            ctx.baseline_candidate,
            ctx.cancel,
        )
        ctx.store.update(ctx.task_id, regression_baseline=library)
    regressions = ctx.verifier.regressions(
        VerificationRequest.from_task(ctx.store.get(ctx.task_id)),
        candidate,
        ctx.cancel,
        library["case_ids"],
    )
    attempt["repository_regressions"] = regressions
    attempt["regressions"] = sorted(set(library["passed_cases"]) - set(regressions["passed_cases"]))
    ctx.save(state, attempt)
    if attempt["regressions"]:
        raise HarnessError("product_failure", "候选出现仓库回归")


@node
def confirmation(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    task, attempt, plan, candidate = ctx.inputs(state)
    number = state["attempt"]
    if plan.real_agent:
        confirmation = evaluate(ctx, candidate, plan, f"confirm-{number}", list(plan.checks))
        attempt["confirmation"] = confirmation
        ctx.save(state, attempt)
        if not (set(plan.primary_checks) | set(task["protected_cases"])).issubset(
            confirmation["passed_cases"]
        ):
            raise HarnessError("product_failure", "独立确认未通过")
    if not ctx.governance and set(plan.primary_checks).issubset(
        attempt["reproduction"]["passed_cases"]
    ):
        improvement = ctx.scenario.purpose == "improvement"
        attempt["gaps"].append(
            {
                "requirement": "expected_behavior" if improvement else "baseline_failure",
                "code": "unverified",
                "message": "冻结目标在旧版本也通过，尚未证明需求带来的能力差异" if improvement else "原始基线未复现目标失败",
            }
        )
        ctx.save(state, attempt)
    return {
        "confirmation_ref": ctx.put(
            "confirmation", state["attempt"], ctx.attempt(state).get("confirmation")
        )
    }


@node
def review(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    task, attempt, plan, candidate = ctx.inputs(state)
    number = state["attempt"]
    source, requirements = (task["source"], task["acceptance"])
    reproduction, verification = (attempt["reproduction"], attempt["verification"])
    library, regressions = (task["regression_baseline"], attempt["repository_regressions"])
    protected, gaps = (task["protected_cases"], attempt["gaps"])
    baseline = ctx.baseline_candidate
    worktree = ctx.artifacts.worktree
    output = ctx.artifacts.attempt_directory(number)
    remaining = ctx.remaining
    ctx.store.update(ctx.task_id, stage="review")
    review_evidence = {
        "source": source,
        "acceptance": requirements,
        "baseline_root": str(baseline.path),
        "reproduction": reproduction,
        "verification": {
            "target": {
                "required_checks": list(plan.primary_checks),
                "passed_checks": verification["passed_cases"],
                "receipt": verification,
            },
            "confirmation": {
                "required": plan.real_agent,
                "status": "passed"
                if attempt.get("confirmation")
                else "not_required"
                if not plan.real_agent
                else "missing",
                "receipt": attempt.get("confirmation"),
            },
            "repository_baseline": library,
            "repository_regressions": regressions,
            "protected_cases": protected,
        },
        "regression": {"checks": regressions, **ctx.artifacts.regression(source)},
        "gaps": gaps,
        "patch": ctx.artifacts.patch(attempt),
        "task_id": ctx.task_id,
    }
    binding = hashlib.sha256(
        json_text(
            {
                "candidate": candidate.digest,
                "goal": requirements,
                "plan": plan.to_payload(),
                "binding": asdict(ctx.scenario.binding(task)),
                "baseline": reproduction["passed_cases"],
                "verified": verification["passed_cases"],
                "regressions": attempt["regressions"],
                "gaps": gaps,
            }
        ).encode()
    ).hexdigest()
    cached_ref = ctx.store.get(ctx.task_id).get("review_cache", {}).get(binding)
    try:
        cached = ctx.records.read(cached_ref) if cached_ref else None
        reviewed = cached or ctx.roles.review(
            worktree, review_evidence, remaining(), output / "review", ctx.cancel
        )
        decision = review_decision(
            {
                k: reviewed[k]
                for k in ("decision", "problem", "reason", "evidence_refs")
                if k in reviewed
            }
        )
        decision = ctx.scenario.review_extension(
            task, decision, reviewed, baseline.path, candidate.path, attempt["changed_files"]
        )
    except Exception as exc:
        if isinstance(exc, Cancelled) or getattr(exc, "code", "") == "budget_exhausted":
            raise
        attempt["review"] = {
            "decision": "inconclusive",
            "problem": "审核执行未能完成",
            "reason": safe_error(exc),
            "state": "complete",
            "binding": binding,
            "evidence_refs": ["verification"],
        }
        ctx.save(state, attempt)
        raise HarnessError("review_inconclusive", "独立审核未确认；候选已保留") from exc
    attempt["review"] = {
        **decision,
        "binding": binding,
        "state": "complete",
        "cached": bool(cached),
        "execution": {}
        if cached
        else {
            k: reviewed.get("execution", {}).get(k)
            for k in ("usage", "trace", "session")
            if k in reviewed.get("execution", {})
        },
    }
    cache = dict(ctx.store.get(ctx.task_id).get("review_cache", {}))
    cache[binding] = ctx.put("review_decision", number, decision)
    ctx.store.update(ctx.task_id, review_cache=cache)
    if ctx.artifacts.digest(worktree) != candidate.digest:
        raise HarnessError("workspace_changed", "审核期间候选发生变化")
    ctx.artifacts.require_git_identity()
    ctx.save(state, attempt)
    if decision["decision"] != "approved":
        raise HarnessError(
            "review_rejected" if decision["decision"] == "rejected" else "review_inconclusive",
            decision["reason"],
        )
    ctx.save(state, attempt)
    return {"review_ref": ctx.put("review", state["attempt"], ctx.attempt(state).get("review"))}


@node
def seal(state: RepairState, runtime: Runtime[HarnessContext]):
    ctx = runtime.context
    task, attempt, plan, candidate = ctx.inputs(state)
    number = state["attempt"]
    gaps, requirements = (attempt["gaps"], task["acceptance"])
    attempt.update(
        status="needs_review" if gaps else "accepted",
        gaps=gaps,
        finished_at=time.time(),
        counts_toward_budget=True,
    )
    for key in ("error", "error_code", "feedback"):
        attempt.pop(key, None)
    ctx.save(state, attempt)
    if gaps:
        ctx.store.update(ctx.task_id, verification_gaps=gaps)
        ctx.finish("needs_review", "acceptance_gap", "局部候选已验证并审阅；完整目标仍有证据缺口")
        return {"outcome": "needs_review"}
    ctx.store.update(
        ctx.task_id, verification_binding=asdict(ctx.scenario.binding(ctx.store.get(ctx.task_id)))
    )
    receipt = AcceptedCandidate(
        candidate.digest,
        requirements["sha256"],
        acceptance_digest(ctx.store.get(ctx.task_id), attempt),
        attempt["review"]["binding"],
        number,
    )
    ctx.store.update(ctx.task_id, accepted_candidate=asdict(receipt))
    ctx.store.finish_verified(
        ctx.task_id, number, attempt, manifest=ctx.artifacts.manifest(candidate.path)
    )
    return {
        "outcome": "fixed",
        "accepted_candidate_ref": ctx.put("accepted_candidate", number, asdict(receipt)),
    }
