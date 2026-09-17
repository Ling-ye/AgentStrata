"""Repeat the frozen acceptance and independent review after merging a newer main."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import uuid
from typing import Any, Callable

from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest
from chatcopilot.harness.code_health_checks import CodeHealthChecks, compare_verification
from chatcopilot.harness.code_health_workspace import save_patch
from chatcopilot.harness.health_documentation import classify
from chatcopilot.harness.health_ledger import SourceLedger
from chatcopilot.harness.models import CandidateRef, CodingOptions, HarnessError, VerificationPlan, review_decision


def revalidate(store: Any, task_id: str, root: Path, base: Path, coder: Any, verifier: Any,
               check_cancel: Callable[[], None]) -> dict[str, Any]:
    task = store.get(task_id)
    approval = task["publication_candidate"]
    directory = private_directory(store.root / "jobs" / task_id / "revalidation" / uuid.uuid4().hex)
    base_manifest, manifest = source_manifest(base), source_manifest(root)
    frozen = directory / "baseline"
    copy_sources(base, frozen, base_manifest)
    ledger = SourceLedger(frozen, base_manifest, directory / "inventory", Path(task["repository"]))
    # New regression definitions remain identical to the original accepted artifact.
    old = approval["manifest"]
    for name in approval["paths"]:
        if name.startswith("tests/") and name in old:
            if manifest.get(name) != old[name]:
                raise HarnessError("reproducer_changed", "同步主干改变了冻结回归测试")
            ledger.generated_tests[name] = old[name]
            ledger.test_contents[old[name]["sha256"]] = (root / name).read_bytes()
    checks = CodeHealthChecks(directory, Path(task["repository"]))
    checks.bind(ledger, frozen)
    paths = sorted(p for p in base_manifest.keys() | manifest.keys() if base_manifest.get(p) != manifest.get(p))
    if any(p not in approval["paths"] for p in paths):
        raise HarnessError("delivery_scope_changed", "同步主干后的 PR 包含原验收之外的变更")
    patch = directory / "candidate.patch"
    save_patch(root, frozen, paths, patch)
    profile = approval["profile"]
    if profile == "documentation_only":
        proof = classify(frozen, root, paths, base_manifest, manifest)
        if not proof["eligible"]:
            raise HarnessError("delivery_scope_changed", "同步主干后不再是普通说明差异")
        verification = checks.documentation(root, paths, check_cancel)
        if not verification["passed"]:
            raise HarnessError("delivery_revalidation_failed", "同步主干后的文档验收未通过")
    else:
        baseline = checks.verify(base, profile, check_cancel)
        verification = checks.verify(root, profile, check_cancel)
        if compare_verification(baseline, verification) is None:
            raise HarnessError("delivery_revalidation_failed", "同步主干后的仓库验收出现回归或缺失证据")
    trials = []
    if task["source"].get("kind", "evaluation") == "code_health":
        for name in ledger.generated_tests:
            if name.endswith(".py"):
                result = checks.run_test(root, (root / name).read_bytes(), check_cancel)
                if any(r["outcome"] != "passed" for r in result["rows"].values()):
                    raise HarnessError("delivery_revalidation_failed", "同步主干后的冻结回归未通过")
                trials.append(result)
    else:
        plan = VerificationPlan.from_payload(task["verification_plan"])
        expected = set(plan.primary_checks) | set(task.get("protected_cases", []))
        for repetition in range(2 if plan.real_agent else 1):
            check_cancel()
            evaluation_id = "eval-harness-" + task_id[7:] + "-delivery-" + manifest_digest(manifest)[:12] + "-" + str(repetition)
            store.update(task_id, delivery_evaluation={"id": evaluation_id, "digest": manifest_digest(manifest)})
            pending_result = False
            try:
                result = verifier.run(store.get(task_id), CandidateRef(root, manifest_digest(manifest), task["base_commit"]),
                                      evaluation_id, list(plan.checks), check_cancel)
                result.require_valid(list(plan.checks), plan.check_repetitions or plan.repetitions)
                if result.candidate_digest != manifest_digest(manifest) or not expected.issubset(result.passed):
                    raise HarnessError("delivery_revalidation_failed", "同步主干后的目标复测未通过")
                trials.append(asdict(result))
            except HarnessError as exc:
                pending_result = exc.code in {"evaluation_unavailable", "result_pending"}
                raise
            finally:
                if not pending_result:
                    store.update(task_id, delivery_evaluation=None)
        regression = verifier.regressions(store.get(task_id), CandidateRef(root, manifest_digest(manifest), task["base_commit"]), check_cancel)
        previous = task.get("regression_baseline", {}).get("passed_cases", [])
        if not set(previous).issubset(regression.get("passed_cases", [])):
            raise HarnessError("delivery_revalidation_failed", "同步主干后的保护集出现回归")
        trials.append(regression)
    source = {**task["source"], "baseline_root": str(frozen), "repository_context": {
        "directory_kind": "git_worktree", "base_commit": task["delivery"].get("update_base_sha"),
        "original_branch": "main", "git_worktree": str(root), "snapshot_digest": manifest_digest(base_manifest)}}
    result = coder.review(root, {"source": source, "patch": patch.read_text(),
        "reproduction": task.get("evaluations", {}), "verification": verification, "regression": trials},
        CodingOptions(task["options"]["model"], task["options"]["reasoning_effort"], 1, None), directory / "review", check_cancel)
    review = review_decision({key: result[key] for key in ("decision", "problem", "reason", "evidence_refs") if key in result})
    if review["decision"] != "approved" or source_manifest(root) != manifest:
        raise HarnessError("delivery_revalidation_failed", "同步主干后的独立审核未通过或候选已变化")
    record = {"path": directory.relative_to(store.root / "jobs" / task_id).as_posix(), "verification": verification,
              "review": review, "trials": trials, "digest": manifest_digest(manifest)}
    store.update(task_id, delivery_verifications=[*task.get("delivery_verifications", []), record],
                 publication_candidate={**approval, "manifest": manifest, "digest": manifest_digest(manifest), "paths": paths})
    return record
