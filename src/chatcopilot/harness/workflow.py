"""Single-case state machine. External work is performed through two ports."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.harness.models import (
    Cancelled,
    Coder,
    Verifier,
    Publisher,
    CandidateRef,
    VerificationPlan,
    HarnessError,
    RepairOptions,
    safe_error,
)
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness import workspace
from chatcopilot.harness.quality import finish_candidate


def run_task(
    store: HarnessStore,
    task_id: str,
    verifier: Verifier,
    coder: Coder,
    *,
    committer: Publisher,
) -> dict[str, Any]:
    from chatcopilot.core.trace_capture import TraceCapture, capture_scope
    from chatcopilot.core.trace_archive import TraceArchive
    capture = TraceCapture({"kind": "harness", "task_id": task_id, "phase": "workflow",
                            "execution_id": uuid.uuid4().hex})
    root = store.root / "jobs" / task_id / "phase-traces"
    pending = {"trace_ref": capture.ref, "capture_state": "recording", "source": capture.source,
               "started_at": capture.started, "finished_at": None, "expires_at": None}
    store.register_trace(task_id, root, pending)
    status = "failed"
    try:
        with capture_scope(capture):
            result = _run_task(store, task_id, verifier, coder, committer=committer)
        status = result["status"]
        return result
    finally:
        reference = {**pending, "capture_state": "failed"}
        try:
            reference = TraceArchive(root).save(capture, status, retained=True)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Harness workflow trace unavailable")
        try:
            store.register_trace(task_id, root, reference)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Harness workflow trace index unavailable")


def _run_task(
    store: HarnessStore, task_id: str, verifier: Verifier, coder: Coder, *, committer: Publisher,
) -> dict[str, Any]:
    task = store.get(task_id)
    if task["status"] == "cancel_requested":
        return store.update(task_id, status="cancelled", stage="done", message="修复任务已取消")
    if task["status"] not in {"queued", "running"}:
        return task
    started = time.monotonic()
    previous_elapsed = float(task.get("elapsed_seconds", 0))
    options = RepairOptions(**task["options"])
    deadline = started + max(0, options.timeout_seconds - previous_elapsed)
    source = task["source"]
    worktree: Path | None = None
    heartbeat = 0.0

    def check_cancel() -> None:
        nonlocal heartbeat
        current = store.get(task_id)
        if current["status"] == "cancel_requested":
            raise Cancelled()
        if time.monotonic() >= deadline:
            raise HarnessError("budget_exhausted", "本次修复的时间预算已用完")
        if time.monotonic() - heartbeat > 5:
            heartbeat = time.monotonic()
            store.update(
                task_id,
                heartbeat_at=time.time(),
                elapsed_seconds=previous_elapsed + heartbeat - started,
            )

    def candidate() -> CandidateRef:
        assert worktree is not None
        return CandidateRef(worktree, manifest_digest(source_manifest(worktree)), task["base_commit"])

    def evaluate(phase: str, cases: list[str]) -> dict[str, Any]:
        assert worktree is not None and plan is not None
        current = store.get(task_id)
        phases = dict(current.get("evaluations", {}))
        digest = manifest_digest(source_manifest(worktree))
        existing = phases.get(phase, {})
        if existing.get("complete") and not existing.get("retryable"):
            if existing["source_digest"] != digest and phase.startswith(("verify-", "confirm-")):
                raise HarnessError("workspace_changed", "已完成验证与候选不一致")
            if existing.get("error"):
                raise HarnessError(existing["error"]["code"], existing["error"]["message"])
            return existing
        if existing and existing["source_digest"] != digest:
            raise HarnessError("workspace_changed", "恢复测评时发现候选内容变化")
        evaluation_id = existing.get("evaluation_id") or "eval-harness-" + task_id[7:] + "-" + phase
        phases[phase] = {"evaluation_id": evaluation_id, "source_digest": digest, "complete": False}
        store.update(
            task_id,
            evaluations=phases,
            stage=phase,
            current_evaluation_id=evaluation_id,
            working_digest=digest,
        )
        try:
            receipt = verifier.run(store.get(task_id), candidate(), evaluation_id, cases, check_cancel)
            check_cancel()
            if manifest_digest(source_manifest(worktree)) != digest or receipt.candidate_digest != digest:
                raise HarnessError("workspace_changed", "测评期间候选内容变化，结果不能用于验收")
        except Exception as exc:
            retryable = getattr(exc, "code", "") in {"evaluation_unavailable", "result_pending"}
            phases[phase].update(complete=True, retryable=retryable, case_ids=cases, passed_cases=[], failed_cases=cases,
                error={"code": getattr(exc, "code", "execution_error"), "type": type(exc).__name__, "message": safe_error(exc)})
            store.update(task_id, evaluations=phases, current_evaluation_id=evaluation_id if retryable else None)
            raise
        validation_error = None
        try:
            receipt.require_valid(cases, plan.check_repetitions or plan.repetitions)
        except HarnessError as exc:
            validation_error = exc
        passed = receipt.passed
        record = {
            **phases[phase],
            "complete": True,
            "kind": "mixed" if plan.check_repetitions else "agent" if plan.real_agent else "deterministic",
            "test_sha256": source.get("test_sha256"),
            "checks": [asdict(check) for check in receipt.checks],
            "evidence_refs": receipt.evidence_refs,
            "passed_cases": sorted(passed),
            "case_ids": cases,
            "failed_cases": [case for case in cases if case not in passed],
            "target_trials": [check.evidence for check in receipt.checks
                              if check.check_id in plan.primary_checks],
        }
        if validation_error is not None:
            record["error"] = {"code": validation_error.code, "message": str(validation_error)}
        phases[phase] = record
        store.update(task_id, evaluations=phases, current_evaluation_id=None)
        if validation_error is not None:
            raise validation_error
        return record

    plan: VerificationPlan | None = None

    def execute() -> None:
        nonlocal worktree, source, plan
        store.update(task_id, status="running")
        if task.get("commit_intent"):
            worktree = Path(task["worktree"])
            recovered_attempt = next(
                item
                for item in store.attempts(task_id)
                if item["number"] == task["commit_intent"]["attempt"]
            )
            if finish_candidate(
                store, task_id, recovered_attempt, coder, committer, options, check_cancel, deadline
            ):
                store.finish_verified(task_id, recovered_attempt["number"], recovered_attempt)
            return
        check_cancel()
        worktree = workspace.prepare(
            Path(task["repository"]), store.root, task_id, task["base_commit"]
        )
        current_digest = manifest_digest(source_manifest(worktree))
        if task.get("working_digest") and task["working_digest"] != current_digest:
            raise HarnessError("workspace_changed", "工作区被外部修改，不能继续原修复任务")
        baseline = task.get("baseline_manifest") or source_manifest(worktree)
        store.update(
            task_id,
            worktree=str(worktree),
            branch="feat/harness-" + task_id[7:],
            baseline_manifest=baseline,
            working_digest=current_digest,
        )
        current = store.get(task_id)
        if not current.get("preparation_input"):
            store.update(task_id, preparation_input=current["source"])

        def prepare_plan(failure=None):
            nonlocal source, plan
            current = store.get(task_id)
            if failure is not None:
                source_input = {**current["preparation_input"]}
                if current["source"].get("image_resources"):
                    source_input["image_resources"] = current["source"]["image_resources"]
                store.update(task_id, verification_plan=None, source=source_input,
                             preparation_failure=failure, stage="auto_correcting")
            else:
                store.update(task_id, stage="prepare_reproducer")
            source, hypothesis, plan = verifier.prepare(
                store.get(task_id), candidate(), coder,
                replace(options, timeout_seconds=max(1, int(deadline - time.monotonic()))), check_cancel)
            generation = int(current.get("plan_generation", 0))
            if failure is not None or not current.get("verification_plan"):
                generation += 1
            store.update(task_id, source=source, hypothesis=asdict(hypothesis), verification_plan=plan.to_payload(),
                         plan_generation=generation,
                         planned_agent_trials=(2 * options.max_attempts + 1) * sum(
                             plan.check_repetitions.get(name, plan.repetitions) for name in plan.checks
                             if not name.startswith("reproduction")) if plan.real_agent else 0)

        def reproducible_baseline():
            while True:
                generation = store.get(task_id)["plan_generation"]
                phase = "reproduce" if generation == 1 else f"reproduce-r{generation}"
                store.update(task_id, reproduction_phase=phase)
                try:
                    return evaluate(phase, list(plan.primary_checks))
                except HarnessError as exc:
                    if not correctable(exc):
                        raise
                    prepare_plan({"code": exc.code, "message": safe_error(exc),
                                  "evaluation": store.get(task_id)["evaluations"][phase]})

        def correctable(exc):
            return source.get("kind") == "robot_task" and exc.code in {
                "verification_test_definition", "verification_domain_exception", "verification_environment",
                "verification_evidence", "verification_judge", "test_collection_error", "reproduction_error"}

        def verify_candidate(number):
            nonlocal reproduction, source, plan, library, protected
            while True:
                generation = store.get(task_id)["plan_generation"]
                phase = f"verify-{number}" if generation == 1 else f"verify-{number}-r{generation}"
                try:
                    return evaluate(phase, list(plan.checks))
                except HarnessError as exc:
                    if not correctable(exc):
                        raise
                    evidence = {"code": exc.code, "message": safe_error(exc),
                                "evaluation": store.get(task_id)["evaluations"][phase]}
                    patch = store.root / "jobs" / task_id / f"attempt-{number}" / "candidate.patch"
                    from chatcopilot.harness.local_verifier import _read
                    patch_bytes = _read(patch, max_bytes=16 * 1024 * 1024)
                    candidate_digest = candidate().digest
                    workspace.restore(worktree, baseline, candidate_digest)
                    store.update(task_id, working_digest=candidate().digest)
                    try:
                        prepare_plan(evidence)
                        reproduction = reproducible_baseline()
                        if set(plan.primary_checks).issubset(reproduction["passed_cases"]):
                            raise HarnessError("revised_not_reproduced", "修订后的测试未在原基线上复现，不能据此接受已有补丁")
                        remaining = [name for name in plan.checks if name not in plan.primary_checks]
                        revised_baseline = evaluate(f"baseline-r{store.get(task_id)['plan_generation']}", remaining) if remaining else {"passed_cases": []}
                        protected = set(plan.protected_checks) | set(revised_baseline["passed_cases"])
                        prior = store.get(task_id).get("regression_baseline_versions", {})
                        prior[str(generation)] = library
                        library = verifier.regressions(store.get(task_id), candidate(), check_cancel)
                        store.update(task_id, regression_baseline=library, regression_baseline_versions=prior,
                                     protected_cases=sorted(protected))
                    finally:
                        # Restore precisely the saved candidate, without re-executing its coder.
                        subprocess.run(["git", "-C", str(worktree), "apply", "--binary", "-"],
                                       input=patch_bytes, check=True, capture_output=True, timeout=30)
                        if candidate().digest != candidate_digest:
                            raise HarnessError("workspace_changed", "测试修订后候选内容发生变化")
                        store.update(task_id, working_digest=candidate_digest)
                    source = store.get(task_id)["source"]

        prepare_plan()
        reproduction = reproducible_baseline()
        source = store.get(task_id)["source"]
        if set(plan.primary_checks).issubset(reproduction["passed_cases"]):
            store.update(
                task_id,
                status="not_reproduced",
                stage="done",
                message="当前版本未复现；未生成修复补丁",
            )
        else:
            remaining = [case for case in plan.checks if case not in plan.primary_checks]
            baseline_result = evaluate("baseline", remaining) if remaining else {"passed_cases": []}
            protected = set(plan.protected_checks) | set(baseline_result["passed_cases"])
            store.update(task_id, protected_cases=sorted(protected))
            library = store.get(task_id).get("regression_baseline")
            if library is None:
                store.update(task_id, stage="repository_baseline")
                library = verifier.regressions(store.get(task_id), candidate(), check_cancel)
                store.update(task_id, regression_baseline=library)
            for number in range(1, options.max_attempts + 1):
                check_cancel()
                attempts = {item["number"]: item for item in store.attempts(task_id)}
                attempt = attempts.get(number)
                if attempt and attempt["status"] in {"rejected", "coding_failed", "interrupted"}:
                    if manifest_digest(source_manifest(worktree)) != manifest_digest(baseline):
                        workspace.restore(worktree, baseline, store.get(task_id)["working_digest"])
                        store.update(task_id, working_digest=manifest_digest(baseline))
                    continue
                output = store.root / "jobs" / task_id / f"attempt-{number}"
                output.mkdir(mode=0o700, exist_ok=True)
                if not attempt or attempt["status"] not in {
                    "verifying",
                    "reviewing",
                    "review_inconclusive",
                    "committing",
                }:
                    store.update(task_id, stage="coding", current_attempt=number)
                    attempt = {"number": number, "status": "coding", "started_at": time.time()}
                    store.save_attempt(task_id, number, attempt)
                    evidence = {
                        "source": source,
                        "reproduction": reproduction,
                        "previous_attempts": list(attempts.values()),
                        "protected_cases": sorted(protected),
                    }
                    try:
                        coding = coder.run(
                            worktree,
                            evidence,
                            replace(
                                options, timeout_seconds=max(1, int(deadline - time.monotonic()))
                            ),
                            output,
                            check_cancel,
                        )
                        check_cancel()
                        changed = workspace.delta(worktree, baseline, str(source.get("bot_id", "")))
                        if not changed:
                            raise HarnessError("empty_candidate", "编程执行器未生成产品改动")
                        for name in changed:
                            if name.endswith(".py") and (worktree / name).exists():
                                compile((worktree / name).read_bytes(), name, "exec")
                        subprocess.run(
                            ["git", "-C", str(worktree), "diff", "--check"],
                            check=True,
                            capture_output=True,
                            timeout=30,
                        )
                        patch_hash = workspace.save_patch(
                            worktree, baseline, output / "candidate.patch"
                        )
                        digest = manifest_digest(source_manifest(worktree))
                        attempt.update(
                            status="verifying",
                            changed_files=changed,
                            candidate_digest=digest,
                            patch_sha256=patch_hash,
                            coding=coding,
                            checks=["python_syntax", "git_diff_check"],
                        )
                        store.save_attempt(task_id, number, attempt)
                        store.update(task_id, working_digest=digest)
                    except (HarnessError, SyntaxError, subprocess.SubprocessError) as exc:
                        if isinstance(exc, Cancelled) or getattr(exc, "code", "") in {
                            "budget_exhausted",
                            "protected_change",
                        }:
                            raise
                        digest = manifest_digest(source_manifest(worktree))
                        workspace.save_patch(worktree, baseline, output / "candidate.patch")
                        attempt.update(status="coding_failed", error=safe_error(exc))
                        store.save_attempt(task_id, number, attempt)
                        store.update(task_id, working_digest=digest)
                        workspace.restore(worktree, baseline, digest)
                        store.update(task_id, working_digest=manifest_digest(baseline))
                        continue
                verification = verify_candidate(number)
                if library.get("case_ids"):
                    store.update(task_id, stage=f"repository-verify-{number}")
                    regression_result = attempt.get(
                        "repository_regressions"
                    ) or verifier.regressions(
                        store.get(task_id), candidate(), check_cancel, library["case_ids"]
                    )
                    attempt["repository_regressions"] = regression_result
                else:
                    regression_result = {"passed_cases": []}
                passing = set(verification["passed_cases"])
                regressions = sorted(protected - passing)
                regressions.extend(
                    sorted(
                        set(library.get("passed_cases", []))
                        - set(regression_result["passed_cases"])
                    )
                )
                accepted = set(plan.primary_checks).issubset(passing) and not regressions
                coverage = {item: {"checks": checks, "passed": bool(checks) and set(checks).issubset(passing)}
                            for item, checks in plan.coverage.items()}
                if coverage:
                    accepted = accepted and all(item["passed"] for item in coverage.values())
                store.update(task_id, acceptance_coverage=coverage)
                if accepted and plan.real_agent:
                    generation = store.get(task_id)["plan_generation"]
                    confirmation = evaluate(f"confirm-{number}" if generation == 1 else f"confirm-{number}-r{generation}", list(plan.checks))
                    attempt["confirmation"] = confirmation
                    accepted = (set(plan.primary_checks) | protected).issubset(confirmation["passed_cases"])
                attempt.update(
                    status="accepted" if accepted else "rejected",
                    verification=verification,
                    regressions=regressions,
                    finished_at=time.time(),
                )
                if accepted:
                    if finish_candidate(
                        store, task_id, attempt, coder, committer, options, check_cancel, deadline
                    ):
                        store.finish_verified(task_id, number, attempt)
                    break
                store.save_attempt(task_id, number, attempt)
                workspace.restore(worktree, baseline, attempt["candidate_digest"])
                store.update(task_id, working_digest=manifest_digest(baseline))
            else:
                store.update(
                    task_id,
                    status="failed",
                    stage="done",
                    message="已达到尝试次数，目标或回归验收仍未通过",
                )

    try:
        execute()
    except Cancelled:
        store.update(
            task_id,
            status="cancelled",
            message="修复任务已取消；本地提交已创建"
            if store.get(task_id).get("local_commit")
            else "修复任务已取消",
        )
    except Exception as exc:
        code = getattr(exc, "code", "execution_error")
        store.update(
            task_id,
            status="waiting_input" if code == "image_required" else "blocked" if isinstance(exc, HarnessError) else "interrupted",
            next_action="upload_image" if code == "image_required" else "technical_failure",
            error_code=code,
            message=safe_error(exc),
        )
    finally:
        changes: dict[str, Any] = {"elapsed_seconds": previous_elapsed + time.monotonic() - started}
        if worktree is not None and store.get(task_id).get("error_code") != "workspace_changed":
            try:
                changes["working_digest"] = manifest_digest(source_manifest(worktree))
            except (OSError, ValueError):
                pass
        store.update(task_id, **changes)
    result = store.get(task_id)
    directory = store.root / "jobs" / task_id
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    report = {
        key: value for key, value in result.items() if key not in {"baseline_manifest", "source"}
    }
    report["attempts"] = store.attempts(task_id)
    path = directory / "report.json"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json_text(report))
    return result
