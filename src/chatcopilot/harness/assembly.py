"""Worker composition; orchestration itself knows no concrete execution backends."""
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.verification import CaseVerification
from chatcopilot.harness.repair_runtime import run_task as execute


def run_task(store, task_id, evaluator, coder, *, local_verifier=None, committer=None):
    from chatcopilot.harness.delivery import initialize, reconcile
    from chatcopilot.harness.models import Cancelled, HarnessError, PIPELINE_VERSION
    from chatcopilot.harness.config import safe_error
    if store.get(task_id).get("pipeline_version") != PIPELINE_VERSION:
        return store.get(task_id)
    local = local_verifier or LocalVerifier(store.root)
    governance = store.get(task_id)["source"].get("kind") == "code_health"
    verifier_type = CaseVerification
    if governance:
        from chatcopilot.harness.governance_verification import GovernanceVerification
        verifier_type = GovernanceVerification
    verifier = verifier_type(evaluator, local, store)
    try:
        from chatcopilot.harness.task_budget import TaskBudget
        with TaskBudget(store, task_id) as budget:
            budget.check()
            initialize(store, task_id)
            from dataclasses import asdict
            from pathlib import Path
            from chatcopilot.harness.artifact_repository import ArtifactRepository
            artifacts = ArtifactRepository(store.root / "jobs" / task_id)
            frozen = artifacts.directory / "source"
            if not store.get(task_id).get("principles"):
                store.update(task_id, principles=asdict(artifacts.principles(frozen if governance else Path(__file__).resolve().parents[3])))
            if governance and not store.get(task_id).get("governance_context"):
                from chatcopilot.harness.governance_repository import freeze_context
                from chatcopilot.harness.workspace import permitted_change
                current = store.get(task_id)
                manifest = current["baseline_manifest"]
                learning = bool(current["source"].get("skill_learning"))
                context = freeze_context(artifacts, frozen, manifest,
                    [name for name in manifest if permitted_change(name, governance=True, learning=learning)],
                    artifacts.read(current["principles"]), learning=learning)
                prior = [row for row in store.history(context_key=current["context_key"])
                         if row["task_id"] != task_id and row.get("governance_report")]
                recent = None
                if prior:
                    previous = prior[0]
                    report = ArtifactRepository(store.root / "jobs" / previous["task_id"]).read(previous["governance_report"])
                    recent = {"task_id": previous["task_id"], "base_commit": previous["base_commit"],
                              "summary": report["summary"], "findings": report["findings"], "unresolved": report["unresolved"],
                              "uninspected": report["uninspected"]}
                store.update(task_id, governance_context=asdict(context), source={**current["source"],
                             "governance_context": asdict(context), "previous_governance": recent})
            if governance and not store.get(task_id).get("harness_skill"):
                from chatcopilot.harness.skill_context import freeze_skill
                reference = freeze_skill(artifacts, frozen)
                if reference:
                    store.update(task_id, harness_skill=reference)
            from chatcopilot.harness.task_environment import prepare_environment
            store.update(task_id, stage="environment")
            environment = prepare_environment(artifacts.directory, artifacts.directory / "source", budget.check)
            store.update(task_id, environment=environment)
            local.python = environment["python"]
            budget.check()
        execute(store, task_id, verifier, coder)
    except Cancelled as exc:
        store.update(task_id, status="cancelled", stage="done", error_code=exc.code, stop_reason=exc.code,
                     message="修复已取消，候选与证据已保留")
    except HarnessError as exc:
        store.update(task_id, status="blocked", stage="done", error_code=exc.code, message=safe_error(exc))
    return reconcile(store, task_id, coder=coder, verifier=verifier)
