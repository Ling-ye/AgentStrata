"""Bounded repair rounds; candidate code never owns acceptance or publication."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text, storage_error_details
from chatcopilot.harness.task_budget import TaskBudget
from chatcopilot.harness.flow_records import record_step
from chatcopilot.harness.models import acceptance_digest, VerificationRequest, CandidateRef, Cancelled, HarnessError, RepairOptions, VerificationPlan, review_decision
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.preparation import acceptance, require_purpose, verification_purpose
from chatcopilot.harness.repair_types import failure_signature
from chatcopilot.harness.agent_types import AcceptedCandidate
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.role_service import RoleWorkflow
from chatcopilot.harness.control_types import external_evaluation_id
from chatcopilot.harness.context_briefs import build_failure_brief


def _passed_checks(attempt: dict[str, Any]) -> set[str]:
    """Count host-observed check progress even before a whole goal passes."""
    return {f"{phase}:{check}" for phase in ("verification", "confirmation", "repository_regressions")
            for check in (attempt.get(phase, {}).get("passed_cases") or ())}


def run_task(store, task_id, verifier, coder, *, workspace_factory) -> dict[str, Any]:
    task = store.get(task_id)
    if task["status"] not in {"queued", "running", "cancel_requested"}:
        return task
    options = RepairOptions(**task["options"])
    governance = verification_purpose(task["source"]) == "governance"
    budget = TaskBudget(store, task_id)
    budget.__enter__()

    def cancel():
        budget.check()

    def remaining():
        cancel()
        return replace(options, timeout_seconds=max(1, int(budget.remaining)) if budget.remaining is not None else None)

    def finish(status, code="", message=""):
        return store.update(task_id, status=status, stage="done", stop_reason=code or status,
                            error_code=code or None, message=message,
                            next_action="review_candidate" if status == "needs_review" else None)

    def evaluate(candidate, plan, phase, checks):
        ident = "eval-harness-" + task_id[7:] + "-" + phase
        current = store.get(task_id)
        existing = current.get("evaluations", {}).get(phase)
        if existing and existing.get("complete"):
            if existing["source_digest"] != candidate.digest:
                raise HarnessError("workspace_changed", "验证快照已变化")
            if existing.get("error"):
                raise HarnessError(existing["error"]["code"], existing["error"]["message"])
            return existing
        with record_step(store, task_id, phase, "基线对照" if phase.startswith("reproduce") else "候选验证",
                group=f"attempt-{current['current_attempt']}" if current.get("current_attempt") else "baseline",
                attempt=current.get("current_attempt"),
                inputs={"candidate_digest": candidate.digest, "checks": checks},
                locator={"section": "evaluations", "key": phase}) as step:
            source = current["source"]
            external = external_evaluation_id(source, ident)
            store.update(task_id, stage=phase, current_evaluation_id=external)
            # Exceptions retain external execution ownership for reconciliation.
            try:
                result = verifier.run(VerificationRequest.from_task(store.get(task_id)), candidate, ident, checks, cancel)
            except Exception as exc:
                phases = dict(store.get(task_id).get("evaluations", {}))
                phases[phase] = {"evaluation_id": ident, "source_digest": candidate.digest, "complete": False, "retryable": getattr(exc, "code", "") in {"evaluation_unavailable", "result_pending"},
                                "error": {"code": getattr(exc, "code", "execution_error"), "message": safe_error(exc)}}
                store.update(task_id, evaluations=phases)
                raise
            store.update(task_id, current_evaluation_id=None)
            cancel()
            error = None
            try:
                result.require_valid(checks, plan.check_repetitions or plan.repetitions)
            except HarnessError as exc:
                error = exc
            if result.candidate_digest != candidate.digest or artifacts.digest(candidate.path) != candidate.digest:
                raise HarnessError("workspace_changed", "验证期间源码快照变化")
            value = {"evaluation_id": ident, "source_digest": candidate.digest, "complete": True,
                     "test_sha256": source.get("test_sha256"),
                     "target_trials": [row.evidence for row in result.checks if row.check_id in plan.primary_checks],
                     "checks": [asdict(row) for row in result.checks], "case_ids": checks,
                     "passed_cases": sorted(result.passed), "failed_cases": sorted(set(checks) - result.passed),
                     "evidence_refs": result.evidence_refs}
            if error:
                value["error"] = {"code": error.code, "message": safe_error(error)}
            phases = dict(store.get(task_id).get("evaluations", {}))
            phases[phase] = value
            store.update(task_id, evaluations=phases)
            step.conclusion = f"通过 {len(result.passed)} 项，未通过 {len(value['failed_cases'])} 项"
            step.evidence = {"passed_cases": value["passed_cases"], "failed_cases": value["failed_cases"]}
            if error:
                raise error
            return value

    try:
        cancel()
        if not acceptance(task.get("preparation_input") or task["source"])["original"].strip():
            raise HarnessError("goal_missing", "缺少原问题或明确预期；请补充目标后新建任务")
        store.update(task_id, status="running", stage="snapshot")
        artifacts = workspace_factory(task)
        worktree = artifacts.worktree
        roles = RoleWorkflow(store, task_id, coder, ArtifactRepository(artifacts.directory))
        if not task.get("problem_ref"):
            store.update(task_id, problem_ref=asdict(roles.artifacts.put("problem", 1, task.get("preparation_input") or task["source"])))
        if not task.get("principles"):
            raise HarnessError("principles_missing", "任务尚未冻结黄金原则")
        digest = artifacts.digest(worktree)
        if task.get("working_digest") and task["working_digest"] != digest:
            raise HarnessError("workspace_changed", "候选工作区被外部修改")
        baseline = artifacts.snapshot("baseline")
        base_manifest = artifacts.manifest(baseline.path)
        original = task.get("preparation_input") or task["source"]
        starting_source = {**original, **({"goal_capabilities": task["goal_capabilities"]} if task.get("goal_capabilities") else {})}
        store.update(task_id, worktree=str(worktree), branch="feat/harness-" + task_id[7:],
                     baseline_manifest=base_manifest, working_digest=digest, preparation_input=original,
                     acceptance=acceptance(starting_source))
        # Existing Evaluation cases already have a frozen oracle; no model-generated preparation is needed.
        if original.get("kind", "evaluation") == "evaluation":
            existing_proposal = {"coverage": [{"requirement": "expected_behavior", "checks": [original["case_id"]]}]}
            source, existing_plan = verifier.prepare(VerificationRequest.from_task(store.get(task_id)), baseline, artifacts.directory,
                                                                  existing_proposal, cancel)
            store.update(task_id, source=source,
                         verification_plan=existing_plan.to_payload(), reproduction_phase="reproduce", plan_generation=1)
            current_result = evaluate(baseline, existing_plan, "reproduce", list(existing_plan.checks))
            if set(existing_plan.primary_checks).issubset(current_result["passed_cases"]):
                return finish("not_reproduced", message="冻结基线已满足原 Case，未生成修复")
        history = store.attempts(task_id)
        previous = history[-1].get("feedback") if history else None
        previous_checks = _passed_checks(history[-1]) if history else set()
        pending = history[-1] if history and history[-1].get("error_code") in {"evaluation_unavailable", "result_pending"} and task.get("current_evaluation_id") else None
        charged = sum(row.get("counts_toward_budget", True) for row in history)
        first = max((row["number"] for row in history), default=0) + 1
        numbers = ([pending["number"]] if pending else []) + list(range(first, first + max(0, options.max_attempts - charged)))
        for number in numbers:
            cancel()
            output = artifacts.attempt_directory(number)
            replaying = pending is not None and number == pending["number"]
            attempt = dict(pending) if replaying else {"number": number, "status": "coding", "started_at": time.time()}
            store.save_attempt(task_id, number, attempt)
            if not replaying:
                store.update(task_id, stage="coding", current_attempt=number, source=original, verification_plan=None)
            stage = "coding"
            try:
                if replaying:
                    current = store.get(task_id)
                    source, requirements = current["source"], current["acceptance"]
                    plan = VerificationPlan.from_payload(current["verification_plan"])
                    proposal = attempt["submission"]
                else:
                    proposal, draft = roles.prepare_round(worktree, baseline.path, number, previous,
                        remaining, cancel, verifier.capabilities())
                    if draft:
                        artifacts.copy_draft(draft, output / "draft")
                    attempt["submission"] = proposal
                    cancel()
                    attempt.update(artifacts.capture(number, base_manifest))
                    store.update(task_id, working_digest=attempt["candidate_digest"],
                                 candidate_checkpoint={k: attempt[k] for k in ("number", "candidate_digest", "patch_path", "patch_sha256", "changed_files")})
                    if governance and proposal["decision"] in {"no_changes", "needs_review"}:
                        status = proposal["decision"] if not attempt["changed_files"] else "needs_review"
                        attempt.update(status=status, finished_at=time.time())
                        store.save_attempt(task_id, number, attempt)
                        finish(status, message=proposal["summary"])
                        break
                    capabilities = sorted(set(store.get(task_id).get("goal_capabilities", [])) | set(proposal["goal_capabilities"]))
                    source = {**original, **({"goal_capabilities": capabilities} if capabilities else {})}
                    if governance:
                        current_source = store.get(task_id)["source"]
                        source.update({key: current_source[key] for key in ("governance_target", "governance_report")})
                    requirements = acceptance(source)
                    store.update(task_id, source=source, acceptance=requirements, goal_capabilities=capabilities)
                    if proposal["decision"] == "blocked":
                        attempt.update(status="blocked", gaps=proposal["gaps"])
                        store.save_attempt(task_id, number, attempt)
                        finish("blocked", proposal["gaps"][0]["code"], proposal["summary"])
                        break
                    stage = "definition"
                    store.update(task_id, stage=stage)
                    source, plan = verifier.prepare(VerificationRequest.from_task(store.get(task_id)), baseline, output, proposal, cancel)
                    store.update(task_id, source=source, verification_plan=plan.to_payload(),
                                 plan_generation=number, reproduction_phase=f"reproduce-r{number}")
                    attempt["verification_plan"] = plan.to_payload()
                require_purpose(source, plan)
                stage = "baseline"
                reproduction = evaluate(baseline, plan, f"reproduce-r{number}", list(plan.checks))
                source = store.get(task_id)["source"]
                if not attempt["changed_files"] and set(plan.primary_checks).issubset(reproduction["passed_cases"]):
                    if governance:
                        raise HarnessError("needs_replan", "治理计划未产生候选变化，不能声明目标已完成")
                    attempt["status"] = "not_reproduced"
                    store.save_attempt(task_id, number, attempt)
                    finish("not_reproduced", message="冻结基线已满足目标检查，未生成修复")
                    break
                if not attempt["changed_files"]:
                    raise HarnessError("empty_candidate", "原问题仍存在，但未生成产品修改")
                protected = sorted(set(plan.protected_checks) | (set(reproduction["passed_cases"]) - set(plan.primary_checks)))
                store.update(task_id, protected_cases=protected)
                candidate = CandidateRef(Path(attempt["snapshot_path"]), attempt["candidate_digest"], task["base_commit"])
                stage = "verification"
                verification = evaluate(candidate, plan, f"verify-{number}", list(plan.checks))
                attempt["verification"] = verification
                missing = sorted((set(plan.primary_checks) | set(protected)) - set(verification["passed_cases"]))
                coverage = {key: {"checks": list(checks), "passed": bool(checks) and set(checks).issubset(verification["passed_cases"])}
                            for key, checks in plan.coverage.items()}
                # Passing a declared check cannot erase an explicit scope/evidence gap.
                gaps = [*proposal["gaps"], *source.get("verification_gaps", [])]
                gaps.extend({"requirement": key, "code": "unverified", "message": "必需目标缺少通过证据"}
                            for key, item in coverage.items() if not item["passed"])
                attempt.update(acceptance_coverage=coverage, gaps=gaps)
                store.update(task_id, acceptance_coverage=coverage, verification_gaps=gaps)
                attempt["regressions"] = sorted(set(protected) - set(verification["passed_cases"]))
                if missing:
                    raise HarnessError("product_failure", "候选目标或保护检查未通过")
                stage = "regressions"
                library = store.get(task_id).get("regression_baseline")
                if library is None:
                    library = verifier.regressions(VerificationRequest.from_task(store.get(task_id)), baseline, cancel)
                    store.update(task_id, regression_baseline=library)
                regressions = verifier.regressions(VerificationRequest.from_task(store.get(task_id)), candidate, cancel, library["case_ids"])
                attempt["repository_regressions"] = regressions
                attempt["regressions"] = sorted(set(library["passed_cases"]) - set(regressions["passed_cases"]))
                if attempt["regressions"]:
                    raise HarnessError("product_failure", "候选出现仓库回归")
                if plan.real_agent:
                    confirmation = evaluate(candidate, plan, f"confirm-{number}", list(plan.checks))
                    attempt["confirmation"] = confirmation
                    if not (set(plan.primary_checks) | set(protected)).issubset(confirmation["passed_cases"]):
                        raise HarnessError("product_failure", "独立确认未通过")
                if not governance and set(plan.primary_checks).issubset(reproduction["passed_cases"]):
                    gaps.append({"requirement": "baseline_failure", "code": "unverified", "message": "原始基线未复现目标失败"})
                stage = "review"
                store.update(task_id, stage=stage)
                review_evidence = {"source": source, "acceptance": requirements, "baseline_root": str(baseline.path), "reproduction": reproduction,
                    "verification": {"target": {"required_checks": list(plan.primary_checks),
                                                   "passed_checks": verification["passed_cases"], "receipt": verification},
                                     "confirmation": {"required": plan.real_agent,
                                                      "status": "passed" if attempt.get("confirmation") else "not_required" if not plan.real_agent else "missing",
                                                      "receipt": attempt.get("confirmation")},
                                     "repository_baseline": library, "repository_regressions": regressions,
                                     "protected_cases": protected},
                    "regression": {"checks": regressions, **artifacts.regression(source)}, "gaps": gaps,
                    "patch": artifacts.patch(attempt), "task_id": task_id}
                binding = hashlib.sha256(json_text({
                    "candidate": candidate.digest, "goal": requirements, "plan": plan.to_payload(),
                    "test": source.get("test_sha256"), "case": source.get("case_snapshot_id") or source.get("agent_source", {}).get("case_snapshot_id"),
                    "conditions": source.get("conditions") or source.get("agent_source", {}).get("conditions"),
                    "baseline": reproduction["passed_cases"], "verified": verification["passed_cases"],
                    "regressions": attempt["regressions"], "gaps": gaps}).encode()).hexdigest()
                cached = store.get(task_id).get("review_cache", {}).get(binding)
                try:
                    reviewed = cached or roles.review(worktree, review_evidence, remaining(), output / "review", cancel)
                    decision = review_decision({k: reviewed[k] for k in ("decision", "problem", "reason", "evidence_refs") if k in reviewed})
                    if governance and decision["decision"] == "approved":
                        from chatcopilot.harness.governance_repository import require_improvement
                        improvement = require_improvement(source["governance_target"], reviewed, baseline.path,
                                                          candidate.path, attempt["changed_files"])
                        decision.update(improvement)
                except Exception as exc:
                    if isinstance(exc, Cancelled) or getattr(exc, "code", "") == "budget_exhausted":
                        raise
                    attempt["review"] = {"decision": "inconclusive", "problem": "审核执行未能完成",
                        "reason": safe_error(exc), "state": "complete", "binding": binding, "evidence_refs": ["verification"]}
                    raise HarnessError("review_inconclusive", "独立审核未确认；候选已保留") from exc
                attempt["review"] = {**decision, "binding": binding, "state": "complete",
                    "cached": bool(cached),
                    "execution": {} if cached else {k: reviewed.get("execution", {}).get(k) for k in ("usage", "trace", "session")
                                                   if k in reviewed.get("execution", {})}}
                cache = dict(store.get(task_id).get("review_cache", {}))
                cache[binding] = decision
                store.update(task_id, review_cache=cache)
                if artifacts.digest(worktree) != candidate.digest:
                    raise HarnessError("workspace_changed", "审核期间候选发生变化")
                artifacts.require_git_identity()
                if decision["decision"] != "approved":
                    raise HarnessError("review_rejected" if decision["decision"] == "rejected" else "review_inconclusive", decision["reason"])
                attempt.update(status="needs_review" if gaps else "accepted", gaps=gaps, finished_at=time.time())
                for key in ("error", "error_code", "feedback"):
                    attempt.pop(key, None)
                store.save_attempt(task_id, number, attempt)
                if gaps:
                    store.update(task_id, verification_gaps=gaps)
                    finish("needs_review", "acceptance_gap", "局部候选已验证并审阅；完整目标仍有证据缺口")
                else:
                    receipt = AcceptedCandidate(candidate.digest, requirements["sha256"],
                        acceptance_digest(store.get(task_id), attempt),
                        binding, number)
                    store.update(task_id, accepted_candidate=asdict(receipt))
                    store.finish_verified(task_id, number, attempt)
                break
            except (HarnessError, SyntaxError, ValueError, OSError) as exc:
                if isinstance(exc, OSError) and storage_error_details(exc):
                    raise
                code = getattr(exc, "code", "test_definition")
                if code in {"test_definition", "invalid_submission", "verification_test_definition"}:
                    stage = "definition"
                elif code in {"needs_replan", "invalid_role_result"}:
                    stage = "plan"
                try:
                    if "candidate_digest" not in attempt:
                        attempt.update(artifacts.capture(number, base_manifest))
                    store.update(task_id, working_digest=attempt["candidate_digest"],
                        candidate_checkpoint={k: attempt[k] for k in ("number", "candidate_digest", "patch_path", "patch_sha256", "changed_files")})
                except (HarnessError, OSError, ValueError):
                    if code not in {"workspace_changed", "protected_change", "index_changed"}:
                        raise
                keys = sorted(k for k, row in attempt.get("acceptance_coverage", {}).items() if not row["passed"]) or [stage]
                signature = failure_signature(stage, code, keys)
                passed = sorted(k for k, row in attempt.get("acceptance_coverage", {}).items() if row["passed"])
                current_checks = _passed_checks(attempt)
                repeated = (previous or {}).get("signature") == signature and not current_checks - previous_checks
                previous_checks = current_checks
                feedback = {"stage": stage, "code": code, "message": safe_error(exc),
                            "requirements": keys, "passed_requirements": passed, "signature": signature}
                attempt.update(status="interrupted" if isinstance(exc, Cancelled) else "rejected", error=safe_error(exc),
                               error_code=code, finished_at=time.time(), feedback=feedback)
                evidence_ref = None
                if getattr(exc, "evidence", None):
                    ref = roles.artifacts.put("verification_error", number, exc.evidence)
                    evidence_ref = asdict(ref)
                brief = build_failure_brief(attempt=attempt, stage=stage, code=code, message=safe_error(exc),
                    signature=signature, evidence_ref=evidence_ref, evidence=getattr(exc, "evidence", None))
                brief_ref = roles.artifacts.put("failure_brief", number, brief)
                attempt["failure_brief"] = asdict(brief_ref)
                attempt["feedback"] = {**feedback, "brief_ref": asdict(brief_ref)}
                store.update(task_id, failure_brief_ref=asdict(brief_ref))
                store.save_attempt(task_id, number, attempt)
                previous = attempt["feedback"]
                if isinstance(exc, Cancelled):
                    raise
                if code in {"budget_exhausted", "no_progress", "fixture_missing", "image_required", "protected_change",
                            "workspace_changed", "index_changed", "governance_target_changed", "governance_run_stopped", "verification_environment", "verification_judge", "verification_evidence",
                            "evaluation_unavailable", "result_pending", "coding_environment", "review_inconclusive", "plan_blocked", "material_missing"}:
                    raise
                if code in {"session_unconfirmed", "session_changed"}:
                    raise
                if repeated:
                    raise HarnessError("no_progress", "连续两轮没有新增有效验收证据") from exc
        else:
            finish("failed", "attempts_exhausted", "已达到完整修复轮次上限；候选与失败证据已保留")
    except Cancelled:
        finish("cancelled", "cancelled", "修复已取消，候选与证据已保留")
    except Exception as exc:
        if isinstance(exc, sqlite3.Error) or storage_error_details(exc):
            raise
        pending_cancel = store.get(task_id)["status"] == "cancel_requested" and getattr(exc, "code", "") in {"evaluation_unavailable", "result_pending"}
        finish("cancel_requested" if pending_cancel else "blocked" if isinstance(exc, HarnessError) else "interrupted",
               getattr(exc, "code", "execution_error"), safe_error(exc))
    finally:
        budget.__exit__()
    return store.get(task_id)
