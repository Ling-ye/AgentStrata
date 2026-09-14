"""Proactive maintenance workflow on the existing Harness task host."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest, verify_copy
from chatcopilot.harness import code_health_workspace as candidate_workspace
from chatcopilot.harness.code_health_checks import CodeHealthChecks, compare_verification
from chatcopilot.harness.code_health_rules import RULES
from chatcopilot.harness.models import ACTIVE, Cancelled, HarnessError, RepairOptions, review_decision, safe_error
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.workspace import prepare


def run_task(store: HarnessStore, task_id: str, coder: Any, *, checks: Any = None) -> dict[str, Any]:
    task = store.get(task_id)
    if task["status"] not in ACTIVE:
        return task
    directory = private_directory(store.root / "jobs" / task_id)
    frozen = directory / "source"
    baseline = task["baseline_manifest"]
    options = RepairOptions(**task["options"])
    started = time.monotonic()
    deadline = started + options.timeout_seconds
    heartbeat = 0.0
    root: Path | None = None
    scope = task["source"]["scope"]
    checks = checks or CodeHealthChecks(directory, Path(task["repository"]))

    def check_cancel() -> None:
        nonlocal heartbeat
        if store.get(task_id)["status"] == "cancel_requested":
            raise Cancelled()
        now = time.monotonic()
        if now >= deadline:
            raise HarnessError("budget_exhausted", "代码治理已达到本次总时限")
        if now - heartbeat > 5:
            heartbeat = now
            store.update(task_id, heartbeat_at=time.time(), elapsed_seconds=now - started)

    def remaining() -> RepairOptions:
        check_cancel()
        return replace(options, timeout_seconds=max(1, int(deadline - time.monotonic())))

    def assert_candidate(expected: str) -> None:
        if root is None or manifest_digest(source_manifest(root)) != expected:
            raise HarnessError("workspace_changed", "候选内容与当前验证证据不一致")
        verify_copy(frozen, baseline)

    try:
        check_cancel()
        store.update(task_id, status="running", stage="snapshot")
        verify_copy(frozen, baseline)
        root = prepare(Path(task["repository"]), store.root, task_id, task["base_commit"])
        candidate_workspace.restore(root, frozen, baseline)
        baseline_digest = manifest_digest(baseline)
        store.update(task_id, worktree=str(root), branch="feat/harness-" + task_id[7:],
                     working_digest=baseline_digest, stage="scan")
        before = checks.scan(root, scope, check_cancel)
        assert_candidate(baseline_digest)
        source = {**task["source"], "baseline_root": str(frozen)}
        evidence = {"source": source, "rules": RULES, "mechanical": before}
        store.update(task_id, stage="audit", governance={"before": before, "findings": before["findings"]})
        audit = coder.audit(root, evidence, remaining(), directory / "audit", check_cancel)
        assert_candidate(baseline_digest)
        findings = list({row["id"]: row for row in [*before["findings"], *audit["findings"]]}.values())
        for row in findings:
            if not row["path"] or not candidate_workspace.permitted(row["path"], root, scope):
                row["disposition"] = "needs_decision"
        eligible = [row for row in findings if row["disposition"] == "candidate"]
        # A single related group keeps the candidate reviewable; other findings remain visible.
        selected = [row for row in eligible if (row["rule_id"], row["path"]) ==
                    (eligible[0]["rule_id"], eligible[0]["path"])] if eligible else []
        selected_ids = [row["id"] for row in selected]
        governance = {"before": before, "findings": findings, "selected_ids": selected_ids,
                      "audit_summary": audit["summary"], "inspected_paths": audit["inspected_paths"],
                      "resolved_ids": []}
        store.update(task_id, governance=governance)
        if not selected:
            check_cancel()
            return store.update(task_id, status="not_reproduced", stage="done",
                                message="扫描完成；没有可自动清理的问题" if findings else "扫描完成；本次未发现问题")

        previous: dict[str, Any] = {}
        baselines: dict[str, dict[str, Any]] = {}
        for number in range(1, options.max_attempts + 1):
            check_cancel()
            output = private_directory(directory / f"attempt-{number}")
            store.update(task_id, stage="coding", current_attempt=number)
            attempt: dict[str, Any] = {"number": number, "status": "coding", "started_at": time.time()}
            store.save_attempt(task_id, number, attempt)
            try:
                attempt["coding"] = coder.run(root, {**evidence, "selected_findings": selected,
                                                     "previous_attempt": previous}, remaining(), output, check_cancel)
                names = candidate_workspace.changes(root, baseline, scope)
                if not names:
                    raise HarnessError("no_changes", "Codex 没有产生候选改动")
                digest = manifest_digest(source_manifest(root))
                attempt.update(changed_files=names, candidate_digest=digest,
                               patch_sha256=candidate_workspace.save_patch(root, frozen, names, output / "candidate.patch"),
                               status="verifying")
                store.save_attempt(task_id, number, attempt)
                store.update(task_id, stage="verify", working_digest=digest)
                after = checks.scan(root, scope, check_cancel)
                assert_candidate(digest)
                before_ids = {row["id"] for row in before["findings"]}
                after_ids = {row["id"] for row in after["findings"]}
                if after_ids - before_ids:
                    raise HarnessError("regression", "候选引入新的确定性检查问题")
                if any(row["detector"] != "codex" and row["id"] in after_ids for row in selected):
                    raise HarnessError("not_fixed", "目标检查问题仍未解决")
                areas = {"/".join(Path(name).parts[:3]) for name in names}
                profile = "full" if (len(areas) > 1 or any(name.startswith("console/") for name in names)
                                      or any(row["rule_id"] == "architecture" for row in selected)) else "fast"
                if profile not in baselines:
                    store.update(task_id, stage="repository_baseline")
                    baselines[profile] = checks.verify(root, profile, check_cancel, baseline=(frozen, baseline))
                    governance["baselines"] = baselines
                    store.update(task_id, governance=governance, stage="verify")
                verification = checks.verify(root, profile, check_cancel)
                assert_candidate(digest)
                retained = compare_verification(baselines[profile], verification)
                verification.update(accepted=retained is not None, retained_failures=retained or [])
                attempt.update(verification=verification, after=after)
                store.save_attempt(task_id, number, attempt)
                if retained is None:
                    raise HarnessError("verification_failed", "候选未通过仓库验收；查看具体检查结果")
                store.update(task_id, stage="review")
                review = coder.review(root, {"source": {**source, "selected_findings": selected},
                                             "reproduction": before, "verification": verification,
                                             "patch": (output / "candidate.patch").read_text(encoding="utf-8", errors="replace"),
                                             "regression": after}, remaining(), output / "review", check_cancel)
                decision = review_decision({key: review[key] for key in ("decision", "problem", "reason", "evidence_refs")})
                assert_candidate(digest)
                attempt["review"] = decision
                if decision["decision"] != "approved":
                    raise HarnessError("review_rejected", decision["problem"] + "；" + decision["reason"])
                check_cancel()
                attempt.update(status="accepted", finished_at=time.time(), regressions=[])
                store.save_attempt(task_id, number, attempt)
                governance.update(after=after, resolved_ids=selected_ids)
                return store.update(task_id, status="fixed", stage="done", governance=governance,
                                    verified_digest=digest, working_digest=digest, verified_at=time.time(),
                                    message="清理候选已验证，等待人工提交" + (f"；保留 {len(retained)} 项既有检查失败，详见验证记录" if retained else ""))
            except HarnessError as exc:
                if isinstance(exc, Cancelled) or exc.code in {"budget_exhausted", "workspace_changed", "protected_change"}:
                    raise
                attempt.update(status="rejected", error_code=exc.code, error=safe_error(exc), finished_at=time.time())
                store.save_attempt(task_id, number, attempt)
                previous = {key: attempt[key] for key in ("error_code", "error", "verification", "review") if key in attempt}
                candidate_workspace.changes(root, baseline, scope)
                candidate_workspace.restore(root, frozen, baseline, expected_digest=manifest_digest(source_manifest(root)))
                store.update(task_id, working_digest=baseline_digest)
        return store.update(task_id, status="failed", stage="done", message="本次候选未通过验收；问题与尝试记录已保留")
    except Cancelled:
        return store.update(task_id, status="cancelled", stage="done", message="代码治理已取消")
    except Exception as exc:
        return store.update(task_id, status="blocked", error_code=getattr(exc, "code", "execution_failed"),
                            message=safe_error(exc))
    finally:
        values: dict[str, Any] = {"elapsed_seconds": time.monotonic() - started, "check_logs": getattr(checks, "logs", [])}
        if root is not None:
            try:
                values["working_digest"] = manifest_digest(source_manifest(root))
            except (OSError, ValueError):
                pass
        store.update(task_id, **values)
