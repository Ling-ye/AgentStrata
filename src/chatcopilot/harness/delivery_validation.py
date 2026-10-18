"""Revalidate main updates through the same acceptance graph as initial repairs."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
from typing import Any, Callable
import uuid

from chatcopilot.core.private_sqlite import json_text, private_directory
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest, verify_copy
from chatcopilot.harness.engine.acceptance_graph import build_acceptance_graph
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.graph_repository import checkpoints
from chatcopilot.harness.graph_service import execute_graph
from chatcopilot.harness.models import CandidateRef, CodingOptions, HarnessError, VerificationCheck, VerificationPlan, VerificationRequest, VerificationResult, review_decision
from chatcopilot.harness.patches import save_patch
from chatcopilot.harness.operation_repository import OperationRepository
from chatcopilot.harness.preparation import require_purpose
from chatcopilot.harness.repository_checks import RepositoryChecks, compare_verification
from chatcopilot.harness.verification_ledger import SourceLedger


class RevalidationOperations:
    def __init__(self, store, task_id, root, base, coder, verifier, cancel, remaining):
        self.store, self.task_id, self.root, self.base = store, task_id, root, base
        self.coder, self.verifier, self.cancel, self.remaining = coder, verifier, cancel, remaining
        self.task = store.get(task_id)
        self.approval = self.task["publication_candidate"]
        self.manifest, self.base_manifest = source_manifest(root), source_manifest(base)
        self.plan = VerificationPlan.from_payload(self.task["verification_plan"])
        require_purpose(self.task["source"], self.plan)
        self.identity = "revalidation:" + task_id + ":" + hashlib.sha256(json_text({
            "candidate": self.manifest, "base": self.base_manifest, "plan": self.plan.to_payload(),
            "test": self.task["source"].get("test_sha256"), "options": self.task["options"],
            "goal": self.task.get("acceptance")}).encode()).hexdigest()
        self.directory = private_directory(store.root / "jobs" / task_id / "revalidation" / self.identity.rsplit(":", 1)[1])
        self.frozen = self.directory / "baseline"
        self.artifacts = ArtifactRepository(store.root / "jobs" / task_id)
        self.operations = OperationRepository(store, task_id)

    def memo(self, name, operation=None, *, uncertain=False, external_id=None):
        key = self.identity + ":" + name
        binding = hashlib.sha256(key.encode()).hexdigest()
        current = self.store.get(self.task_id)
        receipt = self.operations.get(key, binding)
        if receipt:
            if receipt["state"] == "completed":
                return self.artifacts.read(receipt["result_ref"])
            if receipt["state"] == "failed":
                raise HarnessError(receipt["error"]["code"], receipt["error"]["message"])
            if uncertain and not (external_id and name.startswith("trial")
                    and (current.get("delivery_evaluation") or {}).get("id") == external_id):
                raise HarnessError("result_pending" if name.startswith("trial") else "session_unconfirmed",
                                   "交付验收节点终态未知，先核对原执行")
        if operation is None:
            raise HarnessError("verification_evidence", "交付验收缺少已确认的阶段回执：" + name)
        self.operations.start(key, kind="delivery_validation", node=name, attempt=0,
            binding=binding, external_id=external_id)
        value = operation()
        ref = asdict(self.artifacts.put("delivery_" + name, 1, value))
        self.operations.complete(key, binding, ref)
        return value

    def node(self, name, operation):
        def node(state):
            try:
                self.cancel()
                value = operation(state) or {}
                return {"phase": name, **value}
            except HarnessError as exc:
                ref = asdict(self.artifacts.put("delivery_error", 1, {"code": exc.code, "message": safe_error(exc)}))
                return {"error_ref": ref, "phase": name}
        return node

    def definition(self, state):
        if self.frozen.exists():
            verify_copy(self.frozen, self.base_manifest)
        else:
            copy_sources(self.base, self.frozen, self.base_manifest)
        old = self.approval["manifest"]
        for name in self.approval["paths"]:
            if name.startswith("tests/") and name in old and self.manifest.get(name) != old[name]:
                raise HarnessError("reproducer_changed", "同步主干改变了冻结回归测试")
        paths = self.paths()
        if any(path not in self.approval["paths"] for path in paths):
            raise HarnessError("delivery_scope_changed", "同步主干后的 PR 包含原验收之外的变更")
        patch = self.directory / "candidate.patch"
        pending = self.directory / (uuid.uuid4().hex + ".patch")
        save_patch(self.root, self.frozen, paths, pending)
        try:
            if patch.exists():
                if patch.read_bytes() != pending.read_bytes():
                    raise HarnessError("reproducer_changed", "交付复验补丁与冻结候选不符")
            else:
                pending.replace(patch)
        finally:
            pending.unlink(missing_ok=True)
        self.governance_context()
        return {}

    def paths(self):
        return sorted(path for path in self.base_manifest.keys() | self.manifest.keys()
                      if self.base_manifest.get(path) != self.manifest.get(path))

    def checker(self):
        ledger = SourceLedger(self.frozen, self.base_manifest, self.directory / "inventory", Path(self.task["repository"]))
        old = self.approval["manifest"]
        for name in self.approval["paths"]:
            if name.startswith("tests/") and name in old:
                ledger.generated_tests[name] = old[name]
                ledger.test_contents[old[name]["sha256"]] = (self.root / name).read_bytes()
        environment = self.task.get("environment") or {}
        checks = RepositoryChecks(self.directory, Path(self.task["repository"]),
                                  **({"python": environment["python"]} if environment.get("python") else {}))
        checks.bind(ledger, self.frozen)
        return checks

    def baseline(self, state):
        self.memo("baseline", lambda: self.checker().verify(self.base, self.approval["profile"], self.cancel))

    def verification(self, state):
        verified = self.memo("verification", lambda: self.checker().verify(self.root, self.approval["profile"], self.cancel))
        baseline = self.memo("baseline")
        if compare_verification(baseline, verified) is None:
            raise HarnessError("delivery_revalidation_failed", "同步主干后的仓库验收出现回归或缺失证据")
        if self.plan.purpose == "governance":
            self.verifier.delivery_context(self.task_id, manifest_digest(self.manifest), baseline, verified)

    def trial(self, repetition):
        ident = "eval-harness-" + self.task_id[7:] + "-delivery-" + manifest_digest(self.manifest)[:12] + "-" + str(repetition)
        def execute():
            self.store.update(self.task_id, delivery_evaluation={"id": ident, "digest": manifest_digest(self.manifest)})
            result = self.verifier.run(VerificationRequest.from_task(self.store.get(self.task_id)),
                CandidateRef(self.root, manifest_digest(self.manifest), self.task["base_commit"]), ident,
                list(self.plan.checks), self.cancel)
            return asdict(result)
        # Verifier.run reconciles the same stable external evaluation ID; it does
        # not start another evaluation when that ID was already accepted.
        raw = self.memo("trial-" + str(repetition), execute, uncertain=True, external_id=ident)
        self.store.update(self.task_id, delivery_evaluation=None)
        result = VerificationResult(raw["run_id"], raw["candidate_digest"],
                                    tuple(VerificationCheck(**row) for row in raw["checks"]), tuple(raw["evidence_refs"]))
        result.require_bound(CandidateRef(self.root, manifest_digest(self.manifest), self.task["base_commit"]), ident)
        result.require_valid(list(self.plan.checks), self.plan.check_repetitions or self.plan.repetitions)
        if not (set(self.plan.primary_checks) | set(self.task.get("protected_cases", []))).issubset(result.passed):
            raise HarnessError("delivery_revalidation_failed", "同步主干后的目标复测未通过")
        return raw

    def regressions(self, state):
        self.trial(0)
        result = self.memo("regressions", lambda: self.verifier.regressions(
            VerificationRequest.from_task(self.store.get(self.task_id)),
            CandidateRef(self.root, manifest_digest(self.manifest), self.task["base_commit"]), self.cancel))
        if not set(self.task.get("regression_baseline", {}).get("passed_cases", [])).issubset(result.get("passed_cases", [])):
            raise HarnessError("delivery_revalidation_failed", "同步主干后的保护集出现回归")

    def confirmation(self, state):
        if self.plan.real_agent:
            self.trial(1)

    def governance_context(self):
        if self.plan.purpose != "governance":
            return {}
        from chatcopilot.harness.governance_repository import rule_path
        context = self.artifacts.read(self.task["governance_context"])
        rules = {row["path"]: row["sha256"] for row in context["rules"]}
        for reference in self.task["source"]["governance_target"]["principle_refs"]:
            name = rule_path(reference)
            if not (self.frozen / name).is_file() or hashlib.sha256((self.frozen / name).read_bytes()).hexdigest() != rules.get(name):
                raise HarnessError("governance_rules_changed", "主干熵回收规则已变化，请重新调查并形成熵回收任务")
        return {"principles": self.artifacts.navigation(self.task["principles"]),
                "governance_context": self.artifacts.navigation(self.task["governance_context"])}

    def trials(self):
        values = [self.memo("trial-0")]
        if self.plan.real_agent:
            values.append(self.memo("trial-1"))
        return [*values, self.memo("regressions")]

    def review(self, state):
        source = {**self.task["source"], "baseline_root": str(self.frozen), "repository_context": {
            "directory_kind": "git_worktree", "base_commit": self.task["delivery"].get("update_base_sha"),
            "original_branch": "main", "git_worktree": str(self.root), "snapshot_digest": manifest_digest(self.base_manifest)}}
        def execute():
            return self.coder.review(self.root, {"task_id": self.task_id, "source": source,
                "patch": (self.directory / "candidate.patch").read_text(), **self.governance_context(),
                "reproduction": self.task.get("evaluations", {}), "verification": self.memo("verification"),
                "regression": self.trials()}, CodingOptions(self.task["options"]["model"],
                    self.task["options"]["reasoning_effort"], 1,
                    max(1, int(self.remaining())) if self.remaining() is not None else None,
                    self.task["options"].get("model_settings", {}), self.task["options"].get("model_profile", "")),
                self.directory / "review", self.cancel)
        result = self.memo("review", execute, uncertain=True)
        review = review_decision({key: result[key] for key in ("decision", "problem", "reason", "evidence_refs") if key in result})
        if self.plan.purpose == "governance" and review["decision"] == "approved":
            from chatcopilot.harness.governance_repository import require_improvement
            review.update(require_improvement(self.task["source"]["governance_target"], result, self.frozen,
                self.root, [path for path in self.paths() if not path.startswith("tests/")]))
        if review["decision"] != "approved" or source_manifest(self.root) != self.manifest:
            raise HarnessError("delivery_revalidation_failed", "同步主干后的独立审核未通过或候选已变化")
        self.memo("review_verdict", lambda: review)

    def seal(self, state):
        record = {"path": self.directory.relative_to(self.store.root / "jobs" / self.task_id).as_posix(),
            "verification": self.memo("verification"), "review": self.memo("review_verdict"),
            "trials": self.trials(), "digest": manifest_digest(self.manifest), "graph_identity": self.identity}
        task = self.store.get(self.task_id)
        previous = [row for row in task.get("delivery_verifications", []) if row.get("graph_identity") != self.identity]
        self.store.update(self.task_id, delivery_verifications=[*previous, record], publication_candidate={
            **self.approval, "manifest": self.manifest, "digest": manifest_digest(self.manifest), "paths": self.paths(),
            "acceptance_binding": hashlib.sha256(json_text(record).encode()).hexdigest()})
        return {}


def revalidate(store: Any, task_id: str, root: Path, base: Path, coder: Any, verifier: Any,
               check_cancel: Callable[[], None], *, remaining: Callable[[], float | None] = lambda: None) -> dict[str, Any]:
    operations = RevalidationOperations(store, task_id, root, base, coder, verifier, check_cancel, remaining)
    with checkpoints(store.root, operations.identity) as saver:
        graph = build_acceptance_graph(tuple(operations.node(name, operation) for name, operation in (
            ("definition", operations.definition), ("baseline", operations.baseline),
            ("verification", operations.verification), ("regressions", operations.regressions),
            ("confirmation", operations.confirmation), ("review", operations.review), ("seal", operations.seal))))
        # A standalone acceptance invocation needs its own persistent root.
        from langgraph.graph import END, START, StateGraph
        from chatcopilot.harness.engine.state import AcceptanceState
        builder = StateGraph(AcceptanceState)
        builder.add_node("acceptance", graph)
        builder.add_edge(START, "acceptance")
        builder.add_edge("acceptance", END)
        result = execute_graph(builder.compile(checkpointer=saver), operations.identity, initial={})
    if result.get("error_ref"):
        error = operations.artifacts.read(result["error_ref"])
        raise HarnessError(error["code"], error["message"])
    return next(row for row in store.get(task_id)["delivery_verifications"] if row.get("graph_identity") == operations.identity)
