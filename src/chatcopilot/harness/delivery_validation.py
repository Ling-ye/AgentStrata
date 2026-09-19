"""Repeat the frozen acceptance and independent review after merging a newer main."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import uuid
from typing import Any, Callable

from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest
from chatcopilot.harness.repository_checks import RepositoryChecks, compare_verification
from chatcopilot.harness.patches import save_patch
from chatcopilot.harness.verification_ledger import SourceLedger
from chatcopilot.harness.models import VerificationRequest, CandidateRef, CodingOptions, HarnessError, VerificationPlan, review_decision


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
    environment = task.get("environment") or {}
    checks = RepositoryChecks(directory, Path(task["repository"]),
                              **({"python": environment["python"]} if environment.get("python") else {}))
    checks.bind(ledger, frozen)
    paths = sorted(p for p in base_manifest.keys() | manifest.keys() if base_manifest.get(p) != manifest.get(p))
    if any(p not in approval["paths"] for p in paths):
        raise HarnessError("delivery_scope_changed", "同步主干后的 PR 包含原验收之外的变更")
    patch = directory / "candidate.patch"
    save_patch(root, frozen, paths, patch)
    profile = approval["profile"]
    baseline = checks.verify(base, profile, check_cancel)
    verification = checks.verify(root, profile, check_cancel)
    if compare_verification(baseline, verification) is None:
        raise HarnessError("delivery_revalidation_failed", "同步主干后的仓库验收出现回归或缺失证据")
    trials = []
    plan = VerificationPlan.from_payload(task["verification_plan"])
    from chatcopilot.harness.preparation import require_purpose
    require_purpose(task["source"], plan)
    governance_context = {}
    if plan.purpose == "governance":
        from chatcopilot.harness.artifact_repository import ArtifactRepository
        from chatcopilot.harness.governance_repository import rule_path
        import hashlib
        artifacts = ArtifactRepository(store.root / "jobs" / task_id)
        context = artifacts.read(task["governance_context"])
        rules = {row["path"]: row["sha256"] for row in context["rules"]}
        for reference in task["source"]["governance_target"]["principle_refs"]:
            name = rule_path(reference)
            if not (frozen / name).is_file() or hashlib.sha256((frozen / name).read_bytes()).hexdigest() != rules.get(name):
                raise HarnessError("governance_rules_changed", "主干治理规则已变化，请重新调查并形成治理任务")
        governance_context = {"principles": artifacts.navigation(task["principles"]),
                              "governance_context": artifacts.navigation(task["governance_context"])}
        verifier.delivery_context(task_id, manifest_digest(manifest), baseline, verification)
    expected = set(plan.primary_checks) | set(task.get("protected_cases", []))
    for repetition in range(2 if plan.real_agent else 1):
        check_cancel()
        evaluation_id = "eval-harness-" + task_id[7:] + "-delivery-" + manifest_digest(manifest)[:12] + "-" + str(repetition)
        store.update(task_id, delivery_evaluation={"id": evaluation_id, "digest": manifest_digest(manifest)})
        # A returned result confirms execution finished. An exception (including
        # host storage failure) does not, so leave ownership for reconciliation.
        result = verifier.run(VerificationRequest.from_task(store.get(task_id)), CandidateRef(root, manifest_digest(manifest), task["base_commit"]),
                              evaluation_id, list(plan.checks), check_cancel)
        store.update(task_id, delivery_evaluation=None)
        result.require_valid(list(plan.checks), plan.check_repetitions or plan.repetitions)
        if result.candidate_digest != manifest_digest(manifest) or not expected.issubset(result.passed):
            raise HarnessError("delivery_revalidation_failed", "同步主干后的目标复测未通过")
        trials.append(asdict(result))
    regression = verifier.regressions(VerificationRequest.from_task(store.get(task_id)), CandidateRef(root, manifest_digest(manifest), task["base_commit"]), check_cancel)
    previous = task.get("regression_baseline", {}).get("passed_cases", [])
    if not set(previous).issubset(regression.get("passed_cases", [])):
        raise HarnessError("delivery_revalidation_failed", "同步主干后的保护集出现回归")
    trials.append(regression)
    source = {**task["source"], "baseline_root": str(frozen), "repository_context": {
        "directory_kind": "git_worktree", "base_commit": task["delivery"].get("update_base_sha"),
        "original_branch": "main", "git_worktree": str(root), "snapshot_digest": manifest_digest(base_manifest)}}
    result = coder.review(root, {"task_id": task_id, "source": source, "patch": patch.read_text(), **governance_context,
        "reproduction": task.get("evaluations", {}), "verification": verification, "regression": trials},
        CodingOptions(task["options"]["model"], task["options"]["reasoning_effort"], 1, None), directory / "review", check_cancel)
    review = review_decision({key: result[key] for key in ("decision", "problem", "reason", "evidence_refs") if key in result})
    if plan.purpose == "governance" and review["decision"] == "approved":
        from chatcopilot.harness.governance_repository import require_improvement
        product_paths = [name for name in paths if not name.startswith("tests/")]
        review.update(require_improvement(task["source"]["governance_target"], result, frozen, root, product_paths))
    if review["decision"] != "approved" or source_manifest(root) != manifest:
        raise HarnessError("delivery_revalidation_failed", "同步主干后的独立审核未通过或候选已变化")
    record = {"path": directory.relative_to(store.root / "jobs" / task_id).as_posix(), "verification": verification,
              "review": review, "trials": trials, "digest": manifest_digest(manifest)}
    store.update(task_id, delivery_verifications=[*task.get("delivery_verifications", []), record],
                 publication_candidate={**approval, "manifest": manifest, "digest": manifest_digest(manifest), "paths": paths})
    return record
