"""One read-only review followed by host-owned local publication."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from chatcopilot.core.private_sqlite import json_text, private_directory, private_file
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.harness.local_commit import regression_content, regression_ref
from chatcopilot.harness.local_verifier import _read
from chatcopilot.harness.models import (
    Cancelled,
    Coder,
    Publisher,
    HarnessError,
    RepairOptions,
    review_decision,
    safe_error,
)
from chatcopilot.harness.store import HarnessStore


def finish_candidate(
    store: HarnessStore,
    task_id: str,
    attempt: dict[str, Any],
    coder: Coder,
    committer: Publisher,
    options: RepairOptions,
    check_cancel: Callable[[], None],
    deadline: float,
) -> bool:
    task = store.get(task_id)
    if not task.get("review_and_commit"):
        return True
    worktree = Path(task["worktree"])
    number = attempt["number"]
    if not task.get("commit_intent"):
        check_cancel()
        if manifest_digest(source_manifest(worktree)) != attempt["candidate_digest"]:
            raise HarnessError("workspace_changed", "审核前候选内容变化")
        reference = regression_ref(task)
        regression = dict(reference)
        if reference["kind"] in {"pytest", "agent_case"}:
            content = regression_content(task, reference)
            if hashlib.sha256(content).hexdigest() != reference["sha256"]:
                raise HarnessError("reproducer_changed", "审核前冻结测试内容变化")
            regression["test"] = content.decode("utf-8")
        patch = store.root / "jobs" / task_id / f"attempt-{number}" / "candidate.patch"
        private_file(patch)
        patch_bytes = _read(patch, max_bytes=16 * 1024 * 1024)
        if hashlib.sha256(patch_bytes).hexdigest() != attempt["patch_sha256"]:
            raise HarnessError("artifact_changed", "候选补丁与验收记录不一致")
        evidence = {
            "source": task["source"],
            "reproduction": task["evaluations"]["reproduce"],
            "verification": {
                "target": attempt["verification"],
                "confirmation": attempt.get("confirmation"),
                "repository_regressions": attempt.get("repository_regressions"),
                "repository_baseline": task.get("regression_baseline"),
                "protected_cases": task.get("protected_cases", []),
            },
            "patch": patch_bytes.decode("utf-8", errors="replace"),
            "regression": regression,
        }
        binding = hashlib.sha256(
            json_text({"evidence": evidence, "candidate": attempt["candidate_digest"]}).encode()
        ).hexdigest()
        review = attempt.get("review")
        if review and review.get("binding") != binding:
            raise HarnessError("review_changed", "审核所依据的证据发生变化")
        if not review:
            review = {"state": "running", "binding": binding, "started_at": time.time()}
            attempt.update(review=review, status="reviewing")
            store.save_attempt(task_id, number, attempt)
            store.update(task_id, stage="review")
            directory = private_directory(
                store.root / "jobs" / task_id / f"attempt-{number}" / "review"
            )
            try:
                result = coder.review(
                    worktree,
                    evidence,
                    replace(options, timeout_seconds=max(1, int(deadline - time.monotonic()))),
                    directory,
                    check_cancel,
                )
                decision = review_decision(
                    {
                        key: result[key]
                        for key in ("decision", "problem", "reason", "evidence_refs")
                        if key in result
                    }
                )
                review = {
                    **review,
                    **decision,
                    "state": "complete",
                    "finished_at": time.time(),
                    "execution": result.get("execution", {}),
                }
            except Exception as exc:
                review = {
                    **review,
                    "state": "complete",
                    "decision": "inconclusive",
                    "problem": "审核未能完成",
                    "reason": safe_error(exc),
                    "evidence_refs": ["verification"],
                    "finished_at": time.time(),
                }
                attempt["review"] = review
                store.save_attempt(task_id, number, attempt)
                if isinstance(exc, Cancelled):
                    raise
            attempt["review"] = review
            store.save_attempt(task_id, number, attempt)
        elif review["state"] == "running":
            review = {
                **review,
                "state": "complete",
                "decision": "inconclusive",
                "problem": "审核执行已中断",
                "reason": "没有可靠落盘的审核结果；本任务不自动重新审核",
                "evidence_refs": ["verification"],
            }
            attempt["review"] = review
            store.save_attempt(task_id, number, attempt)
        if review["decision"] != "approved":
            rejected = review["decision"] == "rejected"
            attempt["status"] = "review_rejected" if rejected else "review_inconclusive"
            store.save_attempt(task_id, number, attempt)
            store.update(
                task_id,
                status="failed" if rejected else "blocked",
                stage="review",
                error_code="review_rejected" if rejected else "review_inconclusive",
                message=("AI 审核认为问题未解决：" if rejected else "AI 审核未能确认修复：")
                + review["problem"]
                + "；"
                + review["reason"],
            )
            return False
        if manifest_digest(source_manifest(worktree)) != attempt["candidate_digest"]:
            raise HarnessError("workspace_changed", "审核期间候选内容发生变化")
    attempt["status"] = "committing"
    store.save_attempt(task_id, number, attempt)
    store.update(task_id, stage="commit")
    receipt = committer.publish(store, task_id, attempt, check_cancel)
    attempt.update(
        status="accepted", local_commit=receipt, delivered_digest=receipt["content_digest"]
    )
    store.save_attempt(task_id, number, attempt)
    return True
