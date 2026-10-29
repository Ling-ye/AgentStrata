"""Resume frozen acceptance after a confirmed environment or regression-budget stop."""

from dataclasses import asdict
from pathlib import Path

from chatcopilot.core.source_snapshot import manifest_digest, source_manifest
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.engine.repair_graph import build_task_graph
from chatcopilot.harness.engine.evaluation import confirmation_phase
from chatcopilot.harness.graph_repository import checkpoints
from chatcopilot.harness.integrations.scenarios import ImprovementScenario, RepairScenario
from chatcopilot.harness.models import HarnessError


def prepare_acceptance_retry(store, task_id):
    """Called under the lifecycle guard, after proving worker and Evaluation stopped."""
    task = store.get(task_id)
    identity = "task:" + task_id
    with checkpoints(store.root, identity) as saver:
        graph = build_task_graph(saver)
        config = {"configurable": {"thread_id": identity}}
        snapshot = graph.get_state(config)
        if snapshot.next or snapshot.tasks:
            return  # An already prepared fork or interrupted node owns the cursor.
        records = ArtifactRepository(store.root / "jobs" / task_id)
        error = records.read(snapshot.values["error_ref"]) if snapshot.values.get("error_ref") else {}
        stage = error.get("phase")
        regression_budget = stage == "regressions" and error.get("code") == "budget_exhausted"
        if not regression_budget and not (stage == "confirmation" and error.get("code") == "verification_environment"):
            return
        attempt = next(row for row in store.attempts(task_id) if row["number"] == snapshot.values["attempt"])
        previous = task.get("verification_retries", [])
        checkpoint = snapshot.config["configurable"]["checkpoint_id"]
        executed = previous[:-1] if previous and previous[-1]["checkpoint_id"] == checkpoint else previous
        phase = confirmation_phase({**task, "verification_retries": executed}, attempt["number"])
        confirmation = task.get("evaluations", {}).get(phase, {})
        verification = attempt.get("verification", {})
        prefix = "eval-harness-" + task_id[7:] + "-"
        failed_id = confirmation.get("evaluation_id", "")
        phase = failed_id[len(prefix):] if failed_id.startswith(prefix) else ""
        receipt = task.get("operations", {}).get("verification:" + phase, {})
        required = set(task["verification_plan"]["primary_checks"]) | set(task.get("protected_cases", []))
        scenario = ImprovementScenario() if task["source"].get("kind") == "application" else RepairScenario()
        confirmation_valid = (
            confirmation.get("complete")
            and confirmation.get("error", {}).get("code") == "verification_environment"
            and all(row["outcome"] == "passed" or
                    (row["outcome"] == "error" and row["failure_kind"] == "environment")
                    for row in confirmation.get("checks", []))
            and receipt.get("state") == "completed"
            and records.read(receipt["result_ref"]) == confirmation
            and receipt.get("observed_binding") == asdict(scenario.binding(task))
            and "repository_regressions" in attempt and "regression_baseline" in task
            and set(task["regression_baseline"]["passed_cases"]).issubset(
                attempt["repository_regressions"]["passed_cases"])
        )
        budget_available = task["options"]["timeout_seconds"] is None or task["options"]["timeout_seconds"] > task.get("elapsed_seconds", 0)
        valid = (
            (budget_available and not attempt.get("regressions") if regression_budget else confirmation_valid)
            and verification.get("complete") and not verification.get("error")
            and required.issubset(verification.get("passed_cases", []))
            and asdict(scenario.binding(task)) == task.get("verification_binding")
            and records.read(snapshot.values["plan_ref"]) == task["verification_plan"]
            and records.read(snapshot.values["goal_ref"]) == task["acceptance"]
            and manifest_digest(source_manifest(Path(attempt["snapshot_path"]))) == attempt["candidate_digest"]
            and manifest_digest(source_manifest(store.root / "jobs" / task_id / "snapshots/baseline"))
                == attempt["reproduction"]["source_digest"]
            and task.get("working_digest") == attempt["candidate_digest"]
        )
        if not valid:
            raise HarnessError("verification_evidence", "独立确认重验缺少同一候选、冻结条件或已完成保护集证据")
        if previous and previous[-1]["checkpoint_id"] == checkpoint:
            intent = previous[-1]  # A crash before update_state must not allocate another retry.
        else:
            intent = {"sequence": len(previous) + 1, "attempt": attempt["number"],
                      "checkpoint_id": checkpoint, "candidate_digest": attempt["candidate_digest"],
                      "failed_evaluation_id": confirmation.get("evaluation_id"),
                      "stage": stage, "stop_code": error["code"]}
            store.update(task_id, verification_retries=[*previous, intent])
        # A supported LangGraph fork preserves the failed checkpoint and its receipts.
        # The same acceptance subgraph runs a new complete confirmation, Review and seal.
        graph.update_state(config, {"error_ref": None, "outcome": "", "retry_from": stage,
                                    "verification_retry": intent["sequence"]}, as_node="assemble")


def reconcile_pytest_gap(store, task_id, gap_digest):
    """Resolve an explicitly selected missing-pytest claim from already observed facts."""
    import hashlib
    from chatcopilot.core.private_sqlite import json_text
    from chatcopilot.harness.local_verifier import _read
    from chatcopilot.harness.models import acceptance_digest, review_binding

    task = store.get(task_id)
    if task["status"] != "needs_review" or task.get("accepted_candidate") or task["acceptance"].get("requires_image"):
        raise HarnessError("verification_evidence", "此入口只核对已审查候选的 pytest 草案声明，不能豁免其他材料")
    attempt = next(row for row in store.attempts(task_id) if row["number"] == task["current_attempt"])
    records = ArtifactRepository(store.root / "jobs" / task_id)
    existing = [ref for ref in attempt.get("material_resolutions", [])
                if records.read(ref).get("gap_digest") == gap_digest]
    reported = attempt.get("reported_gaps", attempt.get("gaps", []))
    matches = [gap for gap in reported
               if hashlib.sha256(json_text(gap).encode()).hexdigest() == gap_digest]
    if not matches or matches[0].get("code") != "fixture_missing":
        raise HarnessError("verification_evidence", "必须明确选择一条 pytest 草案缺失声明")
    gap = matches[0]
    source = task["source"]
    sha = source.get("test_sha256", "")
    expected = records.directory / "reproducer" / "frozen" / sha / "test_reproduction.py"
    candidate = Path(attempt["snapshot_path"])
    scenario = ImprovementScenario() if source.get("kind") == "application" else RepairScenario()
    binding = asdict(scenario.binding(task))
    required = set(task["verification_plan"]["primary_checks"]) | set(task.get("protected_cases", []))
    valid = (
        bool(sha) and Path(source.get("test_path", "")) == expected
        and expected.resolve() == expected and hashlib.sha256(_read(expected)).hexdigest() == sha
        and attempt.get("review", {}).get("decision") == "approved"
        and attempt["review"]["binding"] == review_binding(task, {**attempt, "gaps": reported}, binding)
        and binding == task.get("verification_binding")
        and attempt.get("acceptance_coverage", {}).get(gap["requirement"], {}).get("passed")
        and all(row.get("complete") and not row.get("error") and row.get("test_sha256") == sha
                for row in (attempt.get("reproduction", {}), attempt.get("verification", {}), attempt.get("confirmation", {})))
        and all(required.issubset(attempt[field].get("passed_cases", [])) for field in ("verification", "confirmation"))
        and not attempt.get("regressions")
        and set(task.get("regression_baseline", {}).get("passed_cases", [])).issubset(
            attempt.get("repository_regressions", {}).get("passed_cases", []))
        and "repository_regressions" in attempt
        and manifest_digest(source_manifest(candidate)) == attempt["candidate_digest"] == task["working_digest"]
        and manifest_digest(source_manifest(Path(task["worktree"]))) == attempt["candidate_digest"]
    )
    if not valid:
        raise HarnessError("verification_evidence", "pytest 草案、冻结哈希、完整确认、保护集或审核证据不足")
    identity = "task:" + task_id
    with checkpoints(store.root, identity) as saver:
        graph = build_task_graph(saver)
        config = {"configurable": {"thread_id": identity}}
        snapshot = graph.get_state(config)
        prepared = bool(existing and (snapshot.next or snapshot.tasks) and snapshot.values.get("retry_from") == "seal")
        if not prepared and (snapshot.next or snapshot.tasks or snapshot.values.get("outcome") != "needs_review"):
            raise HarnessError("conflict", "验收图尚未结束，不能核对材料声明")
        if (records.read(snapshot.values["plan_ref"]) != task["verification_plan"]
                or records.read(snapshot.values["goal_ref"]) != task["acceptance"]):
            raise HarnessError("verification_evidence", "冻结目标或验收方案已经变化")
        proof = {"kind": "frozen_pytest_definition", "reported_gap": gap, "gap_digest": gap_digest,
                 "matched_declarations": len(matches),
                 "candidate_digest": attempt["candidate_digest"], "goal_digest": task["acceptance"]["sha256"],
                 "test_sha256": sha, "review_binding": attempt["review"]["binding"],
                 "observed_acceptance_digest": acceptance_digest(task, attempt),
                 "receipts": {field: attempt[field]["result_ref"]
                              for field in ("reproduction", "verification", "confirmation")}}
        if not existing:
            ref = asdict(records.put("material_resolution", attempt["number"], proof))
            attempt["reported_gaps"] = reported
            attempt["material_resolutions"] = [*attempt.get("material_resolutions", []), ref]
            attempt["gaps"] = [item for item in attempt["gaps"] if item != gap]
            store.save_attempt(task_id, attempt["number"], attempt)
        if not prepared:
            graph.update_state(config, {"error_ref": None, "outcome": "", "retry_from": "seal"}, as_node="assemble")
