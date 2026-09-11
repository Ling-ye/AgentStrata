"""Single-case state machine. External work is performed through two ports."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.harness.models import (
    Cancelled,
    Coder,
    Evaluator,
    HarnessError,
    RepairOptions,
    passed_cases,
    safe_error,
)
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness import workspace
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.local_commit import LocalCommitter
from chatcopilot.harness.quality import finish_candidate


def run_task(
    store: HarnessStore,
    task_id: str,
    evaluator: Evaluator,
    coder: Coder,
    *,
    local_verifier: LocalVerifier | None = None,
    committer: LocalCommitter | None = None,
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
    local = (
        (local_verifier or LocalVerifier(store.root))
        if source.get("kind") == "robot_task"
        else None
    )
    repository_verifier = local_verifier or LocalVerifier(store.root)
    publisher = committer or LocalCommitter()
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

    def evaluate(phase: str, cases: list[str]) -> dict[str, Any]:
        assert worktree is not None
        current = store.get(task_id)
        phases = dict(current.get("evaluations", {}))
        digest = manifest_digest(source_manifest(worktree))
        existing = phases.get(phase, {})
        if existing.get("complete"):
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
        validator = local or evaluator
        receipt = validator.run(store.get(task_id), worktree, evaluation_id, cases, check_cancel)
        check_cancel()
        if manifest_digest(source_manifest(worktree)) != digest:
            raise HarnessError("workspace_changed", "测评期间候选内容变化，结果不能用于验收")
        passed = passed_cases(receipt["result"], source["target_id"], cases, source["repetitions"])
        record = {
            **phases[phase],
            "complete": True,
            "kind": "local_pytest" if local else "evaluation",
            "test_sha256": receipt.get("test_sha256"),
            "passed_cases": sorted(passed),
            "case_ids": cases,
            "failed_cases": [case for case in cases if case not in passed],
            "target_trials": [
                trial
                for trial in receipt["result"]["trials"]
                if trial["case_id"] == source["case_id"]
                and trial["target_id"] == source["target_id"]
            ],
        }
        phases[phase] = record
        store.update(task_id, evaluations=phases, current_evaluation_id=None)
        return record

    def execute() -> None:
        nonlocal worktree, source
        store.update(task_id, status="running")
        if task.get("commit_intent"):
            worktree = Path(task["worktree"])
            recovered_attempt = next(
                item
                for item in store.attempts(task_id)
                if item["number"] == task["commit_intent"]["attempt"]
            )
            if finish_candidate(
                store, task_id, recovered_attempt, coder, publisher, options, check_cancel, deadline
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
        if local and not source.get("test_sha256"):
            store.update(task_id, stage="prepare_reproducer")
            source = local.prepare(
                task,
                worktree,
                coder,
                replace(options, timeout_seconds=max(1, int(deadline - time.monotonic()))),
                check_cancel,
            )
            store.update(task_id, source=source)
        reproduction = evaluate("reproduce", [source["case_id"]])
        if source["case_id"] in reproduction["passed_cases"]:
            store.update(
                task_id,
                status="not_reproduced",
                stage="done",
                message="当前版本未复现；未生成修复补丁",
            )
        else:
            remaining = [case for case in source["case_ids"] if case != source["case_id"]]
            baseline_result = evaluate("baseline", remaining) if remaining else {"passed_cases": []}
            protected = set(source["passed_cases"]) | set(baseline_result["passed_cases"])
            store.update(task_id, protected_cases=sorted(protected))
            library: dict[str, Any] = {}
            if not local:
                library = store.get(task_id).get("regression_baseline") or {}
                if not library:
                    library = repository_verifier.regressions(
                        store.get(task_id), worktree, check_cancel
                    )
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
                        changed = workspace.delta(worktree, baseline)
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
                verification = evaluate(f"verify-{number}", source["case_ids"])
                if not local and library.get("case_ids"):
                    regression_result = attempt.get(
                        "repository_regressions"
                    ) or repository_verifier.regressions(
                        store.get(task_id), worktree, check_cancel, library["case_ids"]
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
                accepted = source["case_id"] in passing and not regressions
                attempt.update(
                    status="accepted" if accepted else "rejected",
                    verification=verification,
                    regressions=regressions,
                    finished_at=time.time(),
                )
                if accepted:
                    if finish_candidate(
                        store, task_id, attempt, coder, publisher, options, check_cancel, deadline
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
            status="blocked" if isinstance(exc, HarnessError) else "interrupted",
            error_code=code,
            message=safe_error(exc),
        )
    finally:
        changes: dict[str, Any] = {"elapsed_seconds": previous_elapsed + time.monotonic() - started}
        if local:
            changes["current_evaluation_id"] = None
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
