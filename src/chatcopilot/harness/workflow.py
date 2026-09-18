"""Bounded repair rounds; candidate code never owns acceptance or publication."""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text, private_directory, storage_error_details
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.harness.control_service import check_cancellation
from chatcopilot.harness.flow_records import record_step
from chatcopilot.harness.models import CandidateRef, Cancelled, HarnessError, RepairOptions, VerificationPlan, safe_error, review_decision
from chatcopilot.harness.preparation import acceptance
from chatcopilot.harness.repair_repository import RepairArtifacts
from chatcopilot.harness.repair_types import failure_signature, submission


def run_task(store, task_id, verifier, coder, *, committer=None) -> dict[str, Any]:
    from chatcopilot.core.trace_capture import TraceCapture, capture_scope
    from chatcopilot.core.trace_archive import TraceArchive
    capture = TraceCapture({"kind": "harness", "task_id": task_id, "phase": "workflow", "execution_id": uuid.uuid4().hex})
    root = store.root / "jobs" / task_id / "phase-traces"
    pending = {"trace_ref": capture.ref, "capture_state": "recording", "source": capture.source,
               "started_at": capture.started, "finished_at": None, "expires_at": None}
    store.register_trace(task_id, root, pending)
    status = "failed"
    try:
        with capture_scope(capture):
            result = _run_task(store, task_id, verifier, coder)
        status = result["status"]
        return result
    except (sqlite3.Error, OSError) as exc:
        if not isinstance(exc, sqlite3.Error) and not storage_error_details(exc):
            raise
        interrupted = store.interrupt(task_id, storage_error=exc)
        # The worker has stopped; only reconcile the owned worktree identity, never replay a transaction.
        if interrupted.get("worktree"):
            digest = manifest_digest(source_manifest(Path(interrupted["worktree"])))
            interrupted = store.update(task_id, working_digest=digest)
        return interrupted
    finally:
        try:
            store.register_trace(task_id, root, TraceArchive(root).save(capture, status, retained=True))
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Harness workflow trace unavailable")


def _run_task(store, task_id, verifier, coder) -> dict[str, Any]:
    task = store.get(task_id)
    if task["status"] not in {"queued", "running", "cancel_requested"}:
        return task
    options = RepairOptions(**task["options"])
    started, elapsed = time.monotonic(), float(task.get("elapsed_seconds", 0))
    deadline = started + max(0, options.timeout_seconds - elapsed)
    heartbeat = 0.0

    def cancel():
        nonlocal heartbeat
        check_cancellation(store.get(task_id))
        now = time.monotonic()
        if now >= deadline:
            raise HarnessError("budget_exhausted", "本次修复的时间预算已用完")
        if now - heartbeat >= 5:
            heartbeat = now
            store.update(task_id, heartbeat_at=time.time(), elapsed_seconds=elapsed + now - started,
                         remaining_seconds=max(0, deadline - now))

    def remaining():
        cancel()
        return replace(options, timeout_seconds=max(1, int(deadline - time.monotonic())))

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
            external = ident + "-agent" if source.get("agent_source") else ident if not source.get("test_sha256") else None
            store.update(task_id, stage=phase, current_evaluation_id=external)
            # Exceptions retain external execution ownership for reconciliation.
            try:
                result = verifier.run(store.get(task_id), candidate, ident, checks, cancel)
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
            if result.candidate_digest != candidate.digest or manifest_digest(source_manifest(candidate.path)) != candidate.digest:
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
        artifacts = RepairArtifacts(store.root, task)
        worktree = artifacts.worktree
        digest = manifest_digest(source_manifest(worktree))
        if task.get("working_digest") and task["working_digest"] != digest:
            raise HarnessError("workspace_changed", "候选工作区被外部修改")
        baseline = artifacts.snapshot("baseline")
        base_manifest = source_manifest(baseline.path)
        original = task.get("preparation_input") or task["source"]
        starting_source = {**original, **({"goal_capabilities": task["goal_capabilities"]} if task.get("goal_capabilities") else {})}
        store.update(task_id, worktree=str(worktree), branch="feat/harness-" + task_id[7:],
                     baseline_manifest=base_manifest, working_digest=digest, preparation_input=original,
                     acceptance=acceptance(starting_source))
        # Existing Evaluation cases already have a frozen oracle; no model-generated preparation is needed.
        if original.get("kind", "evaluation") == "evaluation":
            existing_proposal = {"coverage": [{"requirement": "expected_behavior", "checks": [original["case_id"]]}]}
            source, hypothesis, existing_plan = verifier.prepare(store.get(task_id), baseline, artifacts.directory,
                                                                  existing_proposal, cancel)
            store.update(task_id, source=source, hypothesis=asdict(hypothesis),
                         verification_plan=existing_plan.to_payload(), reproduction_phase="reproduce", plan_generation=1)
            current_result = evaluate(baseline, existing_plan, "reproduce", list(existing_plan.checks))
            if set(existing_plan.primary_checks).issubset(current_result["passed_cases"]):
                return finish("not_reproduced", message="冻结基线已满足原 Case，未生成修复")
        history = store.attempts(task_id)
        previous = history[-1].get("feedback") if history else None
        pending = history[-1] if history and history[-1].get("error_code") in {"evaluation_unavailable", "result_pending"} and task.get("current_evaluation_id") else None
        charged = sum(row.get("counts_toward_budget", True) for row in history)
        first = max((row["number"] for row in history), default=0) + 1
        numbers = ([pending["number"]] if pending else []) + list(range(first, first + max(0, options.max_attempts - charged)))
        for number in numbers:
            cancel()
            output = private_directory(artifacts.directory / f"attempt-{number}")
            private_directory(output / "draft")
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
                    with record_step(store, task_id, "coding", "调查并生成候选", group=f"attempt-{number}", attempt=number,
                            inputs={"goal": store.get(task_id)["acceptance"], "previous_failure": previous},
                            locator={"section": "attempts", "number": number, "field": "coding"}, source_id=f"coding-{number}") as step:
                        execution = coder.run(worktree, {"source": original, "acceptance": store.get(task_id)["acceptance"],
                            "repair_v2": True, "task_id": task_id, "previous_failure": previous,
                            "verification_capabilities": verifier.capabilities()}, remaining(), output, cancel)
                        proposal = submission(execution.get("submission"))
                        attempt["coding"] = {k: execution[k] for k in ("usage", "trace", "session") if k in execution}
                        attempt["submission"] = proposal
                        step.conclusion = proposal["summary"]
                    cancel()
                    attempt.update(artifacts.capture(number, base_manifest))
                    store.update(task_id, working_digest=attempt["candidate_digest"],
                                 candidate_checkpoint={k: attempt[k] for k in ("number", "candidate_digest", "patch_path", "patch_sha256", "changed_files")})
                    capabilities = sorted(set(store.get(task_id).get("goal_capabilities", [])) | set(proposal["goal_capabilities"]))
                    source = {**original, **({"goal_capabilities": capabilities} if capabilities else {})}
                    requirements = acceptance(source)
                    store.update(task_id, source=source, acceptance=requirements, goal_capabilities=capabilities)
                    if proposal["decision"] == "blocked":
                        attempt.update(status="blocked", gaps=proposal["gaps"])
                        store.save_attempt(task_id, number, attempt)
                        finish("blocked", proposal["gaps"][0]["code"], proposal["summary"])
                        break
                    stage = "definition"
                    store.update(task_id, stage=stage)
                    source, hypothesis, plan = verifier.prepare(store.get(task_id), baseline, output, proposal, cancel)
                    store.update(task_id, source=source, hypothesis=asdict(hypothesis), verification_plan=plan.to_payload(),
                                 plan_generation=number, reproduction_phase=f"reproduce-r{number}")
                    attempt["verification_plan"] = plan.to_payload()
                stage = "baseline"
                reproduction = evaluate(baseline, plan, f"reproduce-r{number}", list(plan.checks))
                source = store.get(task_id)["source"]
                if not attempt["changed_files"] and set(plan.primary_checks).issubset(reproduction["passed_cases"]):
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
                    library = verifier.regressions(store.get(task_id), baseline, cancel)
                    store.update(task_id, regression_baseline=library)
                regressions = verifier.regressions(store.get(task_id), candidate, cancel, library["case_ids"])
                attempt["repository_regressions"] = regressions
                attempt["regressions"] = sorted(set(library["passed_cases"]) - set(regressions["passed_cases"]))
                if attempt["regressions"]:
                    raise HarnessError("product_failure", "候选出现仓库回归")
                if plan.real_agent:
                    confirmation = evaluate(candidate, plan, f"confirm-{number}", list(plan.checks))
                    attempt["confirmation"] = confirmation
                    if not (set(plan.primary_checks) | set(protected)).issubset(confirmation["passed_cases"]):
                        raise HarnessError("product_failure", "独立确认未通过")
                if set(plan.primary_checks).issubset(reproduction["passed_cases"]):
                    gaps.append({"requirement": "baseline_failure", "code": "unverified", "message": "原始基线未复现目标失败"})
                stage = "review"
                store.update(task_id, stage=stage)
                review_evidence = {"source": source, "acceptance": requirements, "reproduction": reproduction,
                    "verification": {"target": verification, "confirmation": attempt.get("confirmation"),
                                     "repository_baseline": library, "repository_regressions": regressions,
                                     "protected_cases": protected},
                    "regression": {"checks": regressions, **artifacts.regression(source)}, "gaps": gaps,
                    "patch": artifacts.patch(attempt), "repair_v2": True, "task_id": task_id}
                binding = hashlib.sha256(json_text({
                    "candidate": candidate.digest, "goal": requirements, "plan": plan.to_payload(),
                    "test": source.get("test_sha256"), "case": source.get("case_snapshot_id") or source.get("agent_source", {}).get("case_snapshot_id"),
                    "conditions": source.get("conditions") or source.get("agent_source", {}).get("conditions"),
                    "baseline": reproduction["passed_cases"], "verified": verification["passed_cases"],
                    "regressions": attempt["regressions"], "gaps": gaps}).encode()).hexdigest()
                cached = store.get(task_id).get("review_cache", {}).get(binding)
                with record_step(store, task_id, "review", "独立审核", group=f"attempt-{number}", attempt=number,
                        inputs={"binding": binding, "gaps": gaps}, source_id=f"review-{number}",
                        locator={"section": "attempts", "number": number, "field": "review"}) as step:
                    try:
                        reviewed = cached or coder.review(worktree, review_evidence, remaining(), private_directory(output / "review"), cancel)
                        decision = review_decision({k: reviewed[k] for k in ("decision", "problem", "reason", "evidence_refs") if k in reviewed})
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
                    step.conclusion = decision["reason"]
                if manifest_digest(source_manifest(worktree)) != candidate.digest:
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
                    store.finish_verified(task_id, number, attempt)
                break
            except (HarnessError, SyntaxError, ValueError, OSError) as exc:
                if isinstance(exc, OSError) and storage_error_details(exc):
                    raise
                code = getattr(exc, "code", "test_definition")
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
                prior_evidence = (previous or {}).get("passed_requirements", [])
                passed = sorted(k for k, row in attempt.get("acceptance_coverage", {}).items() if row["passed"])
                repeated = (previous or {}).get("signature") == signature and not set(passed) - set(prior_evidence)
                attempt.update(status="interrupted" if isinstance(exc, Cancelled) else "rejected", error=safe_error(exc),
                    error_code=code, finished_at=time.time(), feedback={"stage": stage, "code": code,
                    "message": safe_error(exc), "requirements": keys, "passed_requirements": passed, "signature": signature})
                if getattr(exc, "evidence", None):
                    evidence_path = output / "verification-error.json"
                    evidence_path.write_text(json_text(exc.evidence))
                    evidence_path.chmod(0o600)
                    attempt["feedback"]["evidence_ref"] = {"path": str(evidence_path),
                        "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()}
                store.save_attempt(task_id, number, attempt)
                previous = attempt["feedback"]
                if isinstance(exc, Cancelled):
                    raise
                if code in {"budget_exhausted", "no_progress", "fixture_missing", "image_required", "protected_change",
                            "workspace_changed", "index_changed", "verification_environment", "verification_judge", "verification_evidence",
                            "evaluation_unavailable", "result_pending", "coding_environment", "review_inconclusive"}:
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
        store.update(task_id, elapsed_seconds=elapsed + time.monotonic() - started,
                     remaining_seconds=max(0, deadline - time.monotonic()))
    return store.get(task_id)
