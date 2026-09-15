"""Budgeted maintenance on the existing Harness host, with verified checkpoints."""
from __future__ import annotations

import hashlib
import copy
import logging
import sqlite3
import time
from itertools import groupby
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text, private_directory, storage_error_details
from chatcopilot.core.source_snapshot import manifest_digest, verify_copy
from chatcopilot.harness.code_health_checks import CodeHealthChecks, compare_verification
from chatcopilot.harness.code_health_rules import RULES
from chatcopilot.harness.code_health_workspace import save_patch
from chatcopilot.harness.health_batches import build_batches, descriptor, save_batch, summary
from chatcopilot.harness.health_budget import HealthBudget, BudgetedCoder
from chatcopilot.harness.health_documentation import eligible, classify as classify_documentation
from chatcopilot.harness.health_ledger import SourceLedger
from chatcopilot.harness.health_policy import policy_path, scope_path, requires_regression
from chatcopilot.harness.health_regressions import frozen_content, prepare_regression
from chatcopilot.harness.models import ACTIVE, GOVERNANCE_VERSION, CodeHealthOptions, Cancelled, HarnessError, RepairOptions, review_decision, safe_error
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.workspace import prepare


class HealthRun:
    def __init__(self, store: HarnessStore, task_id: str, coder: Any, checks: Any) -> None:
        self.store, self.ident, self.coder = store, task_id, coder
        self.task = store.get(task_id)
        self.directory = private_directory(store.root / "jobs" / task_id)
        self.frozen = self.directory / "source"
        self.original = self.task["baseline_manifest"]
        self.options = CodeHealthOptions(**self.task["options"])
        self.budget = HealthBudget(self.options)
        self.coder = BudgetedCoder(coder, self.budget)
        self.started = self.budget.started
        self.heartbeat = 0.0
        self.root: Path | None = None
        self.scope = self.task["source"]["scope"]
        self.checks = checks or CodeHealthChecks(self.directory, Path(self.task["repository"]), budget=self.budget)
        self.ledger = SourceLedger(self.frozen, self.original, private_directory(self.directory / "inventory"),
                                   Path(self.task["repository"]))
        self.checks.bind(self.ledger, self.frozen)
        self.checkpoint_root, self.checkpoint_manifest = self.frozen, self.original
        self.governance: dict[str, Any] = {"version": GOVERNANCE_VERSION, "findings": [], "resolved_ids": [], "groups": [],
                                           "coverage": None, "checkpoints": []}
        self.number = 0
        self.sources: list[dict[str, Any]] = []

    def save(self, **values: Any) -> None:
        self.store.update(self.ident, governance=self.governance, governance_summary=summary(self.governance),
                          check_logs=self.checks.logs, **values)

    def cancel(self) -> None:
        if self.store.get(self.ident)["status"] == "cancel_requested":
            raise Cancelled()
        now = time.monotonic()
        self.budget.check()
        if now - self.heartbeat > 5:
            self.heartbeat = now
            self.save(heartbeat_at=time.time(), elapsed_seconds=now - self.started)

    def remaining(self) -> RepairOptions:
        self.cancel()
        return RepairOptions(self.options.model, self.options.reasoning_effort, self.options.max_attempts,
                             self.options.step_timeout_seconds)

    def target_reached(self) -> bool:
        return (self.options.budget["mode"] == "fixed_groups"
                and summary(self.governance)["accepted_groups"] >= self.options.budget["count"])

    def finish(self, reason: str) -> None:
        counts = summary(self.governance)
        self.save(status="fixed", stage="done", stop_reason=reason,
                  message=f"已验收 {counts['accepted_groups']} 个问题组，修复 {counts['fixed']} 项；"
                          f"范围巡检 {counts['completed_batches']}/{counts['total_batches']} 批")

    def assert_source(self, digest: str) -> None:
        if manifest_digest(self.ledger.manifest(self.root)) != digest:
            raise HarnessError("workspace_changed", "候选内容与当前验证证据不一致")
        verify_copy(self.frozen, self.original)
        verify_copy(self.checkpoint_root, self.checkpoint_manifest)

    def record_call(self, output: Path, kind: str, label: str) -> None:
        private_directory(output)
        relative = output.relative_to(self.directory).as_posix()
        record = {"id": relative, "path": relative + "/public-events.jsonl", "kind": kind,
                  "label": label, "number": self.number or None}
        self.sources.append(record)
        self.save(stage="prepare_reproducer" if kind == "prepare" else kind, progress_sources=self.sources, current_source=relative)

    def rollback(self, *, persist: bool = True) -> None:
        self.ledger.generated_tests = {n: r for n, r in self.ledger.generated_tests.items()
                                       if self.checkpoint_manifest.get(n) == r}
        self.ledger.install(self.root, self.checkpoint_root, self.checkpoint_manifest)
        if persist:
            self.save(working_digest=manifest_digest(self.checkpoint_manifest))

    def add_findings(self, rows: list[dict[str, Any]]) -> None:
        existing = {row["id"] for row in self.governance["findings"]}
        groups = {g["key"]: g for g in self.governance["groups"]}
        for row in rows:
            if row["path"] and not scope_path(row["path"], self.scope):
                continue
            if row["id"] in existing:
                continue
            existing.add(row["id"])
            row = dict(row)
            if not row["path"] or policy_path(row["path"]) or not scope_path(row["path"], self.scope):
                row["disposition"] = "needs_decision"
            self.governance["findings"].append(row)
            key = row.get("group_key") or row["rule_id"] + ":" + row["path"]
            group = groups.get(key)
            # A group already delivered keeps its evidence immutable; a later discovery is new work.
            if group and group["status"] not in {"pending", "needs_decision"}:
                key += ":" + row["id"]
                group = groups.get(key)
            if group is None:
                group = {"id": hashlib.sha256(key.encode()).hexdigest()[:20], "key": key,
                         "finding_ids": [], "depends_on": [], "status": "pending", "attempts": []}
                self.governance["groups"].append(group)
                groups[key] = group
            group["finding_ids"].append(row["id"])
            group["depends_on"] = sorted(set(group["depends_on"]) | set(row.get("depends_on", [])))
            if row["disposition"] == "needs_decision":
                group["status"] = "needs_decision"
        self.save()

    def process_groups(self, *, final: bool = False) -> None:
        made_progress = True
        while made_progress:
            made_progress = False
            groups = {g["key"]: g for g in self.governance["groups"]}
            for group in self.governance["groups"]:
                if group["status"] != "pending":
                    continue
                dependencies = [groups.get(key) for key in group["depends_on"]]
                if any(dep is None or dep["status"] != "accepted" for dep in dependencies):
                    if final:
                        group.update(status="deferred", reason="依赖问题组尚未通过验收")
                    continue
                self.cancel()
                self.process_group(group)
                if self.target_reached():
                    return
                made_progress = True
        self.save(current_group=None)

    def model_source(self, *, snapshot: bool = False) -> dict[str, Any]:
        source = self.task["source"]
        return {**source, "baseline_root": str(self.frozen if snapshot else self.checkpoint_root),
                "repository_context": {"directory_kind": "source_snapshot" if snapshot else "git_worktree",
                    "base_commit": self.task["base_commit"], "original_branch": source.get("original_branch"),
                    "snapshot_digest": source["snapshot_digest"],
                    "checkpoint_digest": manifest_digest(self.original if snapshot else self.checkpoint_manifest),
                    "git_worktree": None if snapshot else str(self.root)}}

    def process_group(self, group: dict[str, Any]) -> None:
        selected = [r for r in self.governance["findings"] if r["id"] in group["finding_ids"]]
        source = self.model_source()
        evidence = {"source": source, "rules": RULES, "selected_findings": selected, "group": group["key"]}
        initial = manifest_digest(self.checkpoint_manifest)
        self.save(current_group=group["id"], current_attempt=None)
        group["status"] = "preparing"
        proof = None
        if eligible(selected) and self.process_documentation(group, evidence, initial):
            return
        try:
            if any(row["detector"] == "codex" or requires_regression(row["path"]) for row in selected):
                proof = prepare_regression(self.root, evidence, private_directory(self.directory / "groups" / group["id"]),
                    self.coder, self.checks, self.remaining, self.cancel, self.record_call,
                    lambda: self.assert_source(initial))
                group["proof"] = proof
                source.update(test_path=proof["path"], test_sha256=proof["sha256"])
            else:
                group["proof"] = {"kind": "mechanical", "checks": sorted({r["detector"] for r in selected})}
        except HarnessError as exc:
            if isinstance(exc, Cancelled) or exc.code in {"budget_exhausted", "step_timeout", "workspace_changed", "policy_change", "coding_environment"}:
                raise
            group.update(status="needs_decision", reason=safe_error(exc), error_code=exc.code)
            for row in selected:
                row["disposition"] = "needs_decision"
            self.save()
            return
        previous: dict[str, Any] = {}
        failures: set[str] = set()
        baselines: dict[str, Any] = {}
        self.save(stage="scan", current_source=None)
        before = self.checks.scan(self.root, self.scope, self.cancel)
        self.assert_source(initial)
        for group_attempt in range(1, self.options.max_attempts + 1):
            self.cancel()
            self.number += 1
            output = private_directory(self.directory / f"attempt-{self.number}")
            attempt: dict[str, Any] = {"version": 2, "number": self.number, "group_id": group["id"],
                "group_attempt": group_attempt, "status": "coding", "started_at": time.time()}
            group["attempts"].append(self.number)
            group["status"] = "coding"
            self.store.save_attempt(self.ident, self.number, attempt)
            self.save(current_attempt=self.number)
            try:
                self.record_call(output, "coding", f"问题组 {group['id']} · 第 {group_attempt} 次修复")
                attempt["coding"] = self.coder.run(self.root, {**evidence, "proof": proof,
                    "previous_attempt": previous}, self.remaining(), output, self.cancel)
                names, manifest = self.ledger.changes(self.root, self.checkpoint_manifest, self.scope)
                if not names:
                    raise HarnessError("no_changes", "Codex 没有产生候选改动")
                if not proof and any(requires_regression(name) for name in names):
                    raise HarnessError("missing_proof", "检查器或控制实现的修改缺少冻结回归依据")
                digest = manifest_digest(manifest)
                self.save(stage="verify", working_digest=digest)
                if proof:
                    content = frozen_content(proof)
                    trial = self.checks.run_test(self.root, content, self.cancel)
                    attempt["regression_test"] = trial
                    self.assert_source(digest)
                    if any(r["outcome"] != "passed" for r in trial["rows"].values()):
                        raise HarnessError("not_fixed", "冻结回归测试未通过")
                    self.ledger.add_test(self.root, content)
                    names, manifest = self.ledger.changes(self.root, self.checkpoint_manifest, self.scope)
                    digest = manifest_digest(manifest)
                attempt.update(changed_files=names, candidate_digest=digest, status="verifying",
                    patch_sha256=save_patch(self.root, self.checkpoint_root, names, output / "candidate.patch"))
                self.store.save_attempt(self.ident, self.number, attempt)
                after = self.checks.scan(self.root, self.scope, self.cancel)
                before_ids, after_ids = ({r["id"] for r in report["findings"]} for report in (before, after))
                if after_ids - before_ids:
                    raise HarnessError("regression", "候选引入新的确定性检查问题")
                if any(r["detector"] != "codex" and r["id"] in after_ids for r in selected):
                    raise HarnessError("not_fixed", "目标检查问题仍未解决")
                areas = {"/".join(Path(n).parts[:3]) for n in names if not n.startswith("tests/")}
                profile = "full" if len(areas) > 1 or any(n.startswith(("console/", "scripts/")) for n in names) or any(r["rule_id"] == "architecture" for r in selected) else "fast"
                if profile not in baselines:
                    self.save(stage="repository_baseline")
                    baselines[profile] = self.checks.verify(self.root, profile, self.cancel,
                        baseline=(self.checkpoint_root, self.checkpoint_manifest))
                    group["baselines"] = baselines
                self.save(stage="verify")
                verification = self.checks.verify(self.root, profile, self.cancel)
                retained = compare_verification(baselines[profile], verification)
                verification.update(accepted=retained is not None, retained_failures=retained or [])
                attempt.update(verification=verification, after=after)
                self.store.save_attempt(self.ident, self.number, attempt)
                self.assert_source(digest)
                if retained is None:
                    raise HarnessError("verification_failed", "候选未通过固定仓库验收；查看具体检查结果")
                self.record_call(output / "review", "review", "候选独立审核")
                review = self.coder.review(self.root, {"source": {**source, "selected_findings": selected},
                    "reproduction": proof or before, "verification": verification,
                    "patch": (output / "candidate.patch").read_text(errors="replace"), "regression": after},
                    self.remaining(), output / "review", self.cancel)
                decision = review_decision({k: review[k] for k in ("decision", "problem", "reason", "evidence_refs")})
                self.assert_source(digest)
                attempt["review"] = decision
                if decision["decision"] != "approved":
                    raise HarnessError("review_rejected", decision["problem"] + "；" + decision["reason"])
                self.cancel()
                self.accept(group, attempt, digest, manifest)
                return
            except HarnessError as exc:
                attempt.update(status="rejected", error_code=exc.code, error=safe_error(exc), finished_at=time.time())
                self.store.save_attempt(self.ident, self.number, attempt)
                if isinstance(exc, Cancelled) or exc.code in {"budget_exhausted", "step_timeout", "workspace_changed", "coding_environment"}:
                    attempt.update(status="interrupted", counts_toward_budget=False)
                    self.store.save_attempt(self.ident, self.number, attempt)
                    raise
                previous = {k: attempt[k] for k in ("error_code", "error", "verification", "review", "regression_test") if k in attempt}
                # Stable error plus candidate bytes: retries require new evidence or a different patch.
                try:
                    candidate_digest = manifest_digest(self.ledger.manifest(self.root))
                except HarnessError:
                    candidate_digest = "unsafe"
                failures_now = [(c["name"], c["exit_code"], c.get("failed_ids", []))
                                for c in attempt.get("verification", {}).get("checks", [])]
                trials_now = [(name, r["outcome"], r.get("exception_chain", []))
                              for name, r in attempt.get("regression_test", {}).get("rows", {}).items()]
                signature = hashlib.sha256(json_text([exc.code, safe_error(exc), candidate_digest,
                    failures_now, trials_now, attempt.get("review")]).encode()).hexdigest()
                repeated = signature in failures
                failures.add(signature)
                self.rollback()
                if repeated or exc.code == "policy_change":
                    group["stop_reason"] = "相同失败且候选没有变化" if repeated else "候选试图修改固定标准"
                    break
        group.update(status="failed", reason=previous.get("error", "候选没有通过验收"))
        self.save()

    def process_documentation(self, group, evidence, initial) -> bool:
        """A rejected boundary reroutes from the checkpoint before standard preparation."""
        previous = {}
        seen = set()
        for group_attempt in range(1, self.options.max_attempts + 1):
            self.cancel()
            self.number += 1
            output = private_directory(self.directory / f"attempt-{self.number}")
            attempt = {"version": GOVERNANCE_VERSION, "number": self.number, "group_id": group["id"],
                       "group_attempt": group_attempt, "status": "coding", "started_at": time.time()}
            group["attempts"].append(self.number)
            group["status"] = "coding"
            self.store.save_attempt(self.ident, self.number, attempt)
            self.save(current_attempt=self.number)
            try:
                self.record_call(output, "coding", "说明类候选")
                attempt["coding"] = self.coder.run(self.root, {**evidence, "verification_route": "documentation_only",
                    "previous_attempt": previous}, self.remaining(), output, self.cancel)
                names, manifest = self.ledger.changes(self.root, self.checkpoint_manifest, self.scope)
                if not names:
                    raise HarnessError("no_changes", "Codex 没有产生候选改动")
                proof = classify_documentation(self.checkpoint_root, self.root, names, self.checkpoint_manifest, manifest)
                if not proof["eligible"]:
                    attempt.update(status="rerouted", route_reason=proof["reason"], counts_toward_budget=False,
                                   finished_at=time.time())
                    self.store.save_attempt(self.ident, self.number, attempt)
                    group["route_reason"] = proof["reason"]
                    self.rollback()
                    self.assert_source(initial)
                    self.save()
                    return False
                group["proof"] = proof
                digest = manifest_digest(manifest)
                attempt.update(changed_files=names, candidate_digest=digest, status="verifying",
                    patch_sha256=save_patch(self.root, self.checkpoint_root, names, output / "candidate.patch"))
                self.store.save_attempt(self.ident, self.number, attempt)
                self.save(stage="verify", working_digest=digest)
                verification = self.checks.documentation(self.root, names, self.cancel)
                verification.update(accepted=verification["passed"], structural_evidence=proof)
                attempt["verification"] = verification
                self.store.save_attempt(self.ident, self.number, attempt)
                self.assert_source(digest)
                if not verification["passed"]:
                    raise HarnessError("verification_failed", "说明类候选未通过定向检查；查看检查日志")
                self.record_call(output / "review", "review", "说明类候选独立审核")
                review = self.coder.review(self.root, {"source": {**evidence["source"], "selected_findings": evidence["selected_findings"]},
                    "reproduction": proof, "verification": verification,
                    "patch": (output / "candidate.patch").read_text(), "regression": {}},
                    self.remaining(), output / "review", self.cancel)
                decision = review_decision({k: review[k] for k in ("decision", "problem", "reason", "evidence_refs")})
                attempt["review"] = decision
                self.assert_source(digest)
                if decision["decision"] != "approved":
                    raise HarnessError("review_rejected", decision["problem"] + "；" + decision["reason"])
                self.cancel()
                self.accept(group, attempt, digest, manifest)
                return True
            except HarnessError as exc:
                if isinstance(exc, Cancelled) or exc.code in {"budget_exhausted", "step_timeout", "workspace_changed", "coding_environment", "policy_change"}:
                    raise
                previous = {"error_code": exc.code, "error": safe_error(exc)}
                attempt.update(status="rejected", **previous, finished_at=time.time())
                self.store.save_attempt(self.ident, self.number, attempt)
                signature = (exc.code, attempt.get("candidate_digest"), safe_error(exc))
                self.rollback()
                if signature in seen:
                    break
                seen.add(signature)
        group.update(status="failed", reason=previous.get("error", "说明候选未通过审核"))
        self.save()
        return True

    def accept(self, group, attempt, digest, manifest) -> None:
        checkpoint = private_directory(self.directory / "checkpoints" / str(self.number))
        self.ledger.checkpoint(self.root, checkpoint / "source", manifest)
        cumulative = sorted(n for n in self.original.keys() | manifest.keys() if self.original.get(n) != manifest.get(n))
        patch_sha = save_patch(self.root, self.frozen, cumulative, checkpoint / "candidate.patch")
        record = {"number": len(self.governance["checkpoints"]) + 1, "attempt": self.number,
                  "group_id": group["id"], "digest": digest, "patch_sha256": patch_sha,
                  "path": checkpoint.relative_to(self.directory).as_posix(), "created_at": time.time(),
                  "changed_files": cumulative}
        previous_governance = copy.deepcopy(self.governance)
        self.governance["checkpoints"].append(record)
        self.governance["resolved_ids"].extend(group["finding_ids"])
        group.update(status="accepted", checkpoint=record["number"])
        attempt.update(status="accepted", finished_at=time.time(), regressions=[])
        try:
            self.save(accepted_attempt=attempt, verified_digest=digest, verified_manifest=manifest,
                      verified_at=time.time(), working_digest=digest, checkpoint=record)
        except Exception:
            self.governance = previous_governance
            raise
        self.checkpoint_root, self.checkpoint_manifest = checkpoint / "source", manifest

    def execute(self) -> None:
        self.cancel()
        self.save(status="running", stage="snapshot")
        verify_copy(self.frozen, self.original)
        self.root = prepare(Path(self.task["repository"]), self.store.root, self.ident, self.task["base_commit"])
        self.rollback()
        self.save(worktree=str(self.root), branch="feat/harness-" + self.ident[7:], stage="scan")
        batches = build_batches(self.frozen, self.original, self.scope)
        self.governance["coverage"] = [descriptor(batch) for batch in batches]
        self.save(stage="scan", current_source=None)
        before = self.checks.scan(self.root, self.scope, self.cancel)
        self.governance["before"] = before
        self.add_findings(before["findings"])
        batch_directory = private_directory(self.directory / "audit-batches")
        for _, domain in groupby(enumerate(batches), key=lambda pair: pair[1]["area"]):
            for index, batch in domain:
                self.cancel()
                entry = self.governance["coverage"][index]
                entry["input"] = "audit-batches/" + save_batch(batch_directory, batch)
                if any(b.get("binary") for b in batch["blocks"]):
                    entry.update(status="unsupported", reason="二进制内容未送入文本巡检，覆盖未完成")
                    self.save()
                    continue
                entry["status"] = "running"
                output = self.directory / "audit" / batch["id"]
                self.record_call(output, "audit", batch["area"])
                try:
                    audit = self.coder.audit(self.frozen, {"source": self.model_source(snapshot=True),
                        "rules": RULES, "batch": batch}, self.remaining(), output, self.cancel)
                    verify_copy(self.frozen, self.original)
                    if audit.get("submitted_batch") != batch["id"] or audit.get("submitted_blocks") != [b["block_sha256"] for b in batch["blocks"]]:
                        raise HarnessError("audit_incomplete", "缺少本批源码块的实际投递记录")
                    entry.update(status="completed", summary=audit["summary"], inspected_paths=audit["inspected_paths"])
                    self.add_findings(audit["findings"])
                except HarnessError as exc:
                    entry.update(status="failed", reason=safe_error(exc), error_code=exc.code)
                    if isinstance(exc, Cancelled) or exc.code in {"budget_exhausted", "step_timeout", "workspace_changed", "coding_environment"}:
                        raise
                    self.save()
            self.process_groups()
            if self.target_reached():
                self.finish("fix_limit_reached")
                return
        self.process_groups(final=True)
        if self.target_reached():
            self.finish("fix_limit_reached")
            return
        self.cancel()
        counts = summary(self.governance)
        status = "fixed" if counts["fixed"] else "not_reproduced" if not counts["found"] else "blocked"
        if any(g["status"] == "failed" for g in self.governance["groups"]) and not counts["fixed"]:
            status = "failed"
        if counts["coverage"] != "complete" and not counts["fixed"]:
            status = "blocked"
        self.save(status=status, stage="done", stop_reason="completed",
                  message=f"发现 {counts['found']} 项，已修复 {counts['fixed']} 项，待判断 {counts['needs_decision']} 项，未处理 {counts['remaining']} 项"
                  + (f"；已验收 {counts['accepted_groups']}/{self.options.budget['count']} 组，当前可执行批次已结束，未达到数量目标"
                     if self.options.budget["mode"] == "fixed_groups" else ""))


def run_task(store: HarnessStore, task_id: str, coder: Any, *, checks: Any = None) -> dict[str, Any]:
    task = store.get(task_id)
    if task["status"] not in ACTIVE or task["source"].get("governance_version") != GOVERNANCE_VERSION:
        return task
    run = HealthRun(store, task_id, coder, checks)
    storage_failure = None
    try:
        run.execute()
    except Exception as exc:
        if isinstance(exc, sqlite3.Error) or storage_error_details(exc):
            storage_failure = exc
        else:
            code = getattr(exc, "code", "execution_failed")
            for batch in run.governance.get("coverage") or []:
                if batch["status"] == "running":
                    batch.update(status="interrupted", reason=safe_error(exc), error_code=code)
            for group in run.governance["groups"]:
                if group["status"] in {"preparing", "coding"}:
                    group.update(status="interrupted", reason=safe_error(exc))
            try:
                for attempt in store.attempts(task_id):
                    if attempt["status"] in {"coding", "verifying", "reviewing"}:
                        attempt.update(status="interrupted", error_code=code, error=safe_error(exc),
                                       counts_toward_budget=False, finished_at=time.time())
                        store.save_attempt(task_id, attempt["number"], attempt)
                latest = store.get(task_id)
                run.save(status="cancelled" if isinstance(exc, Cancelled) else "blocked", stage="done",
                         stop_reason=code, error_code=code, message=safe_error(exc),
                         failure={"code": code, "stage": latest["stage"],
                                  "evidence_source": latest.get("current_source"),
                                  **getattr(exc, "details", {})})
            except (sqlite3.Error, OSError) as error:
                storage_failure = error
    finally:
        if storage_failure is not None:
            try:
                # A commit may finish before close reports an I/O error. Resolve
                # that ambiguity from the durable record before restoring bytes.
                durable = store.get(task_id)
                run.governance = durable.get("governance", run.governance)
                if durable.get("checkpoint"):
                    run.checkpoint_root = run.directory / durable["checkpoint"]["path"] / "source"
                    run.checkpoint_manifest = durable["verified_manifest"]
            except Exception:
                logging.getLogger(__name__).error("Could not read durable checkpoint after storage failure")
        if run.root is not None:
            try:
                run.rollback(persist=False)
            except Exception as exc:
                if storage_failure is not None:
                    logging.getLogger(__name__).error("Checkpoint restore failed after storage failure", exc_info=True)
                else:
                    run.save(status="blocked", error_code="checkpoint_restore_failed",
                             stop_reason="checkpoint_restore_failed", message=safe_error(exc))
    if storage_failure is not None:
        return store.interrupt(task_id, storage_error=storage_failure)
    try:
        run.save(elapsed_seconds=time.monotonic() - run.started, current_group=None, current_source=None, current_attempt=None,
                 working_digest=manifest_digest(run.checkpoint_manifest))
    except (sqlite3.Error, OSError) as error:
        return store.interrupt(task_id, storage_error=error)
    return store.get(task_id)
