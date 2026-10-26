"""Worker composition; orchestration itself knows no concrete execution backends."""
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.repair_runtime import run_task as execute


def controller_dependencies(repository, root, settings, *, evaluator=None, workers=None,
                            task_reader=None, gateway_state_root=None):
    """The public controller's default adapters are composed in one place."""
    from pathlib import Path
    from chatcopilot.harness.store import HarnessStore
    from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
    from chatcopilot.harness.worker_runtime import SystemdWorkerControl
    from chatcopilot.harness.control_service import HarnessLifecycle
    store = HarnessStore(root)
    socket_path = settings.get("CHATCOPILOT_EVALUATION_SOCKET")
    evaluator = evaluator or ServiceEvaluator(socket_path=Path(socket_path) if socket_path else None)
    if gateway_state_root is not None and task_reader is None:
        from chatcopilot.harness.gateway_adapter import read_task
        def task_reader(bot, run):
            return read_task(gateway_state_root, bot, run)
    workers = workers or SystemdWorkerControl(repository, store.root, settings)
    return store, evaluator, task_reader, HarnessLifecycle(store, workers, evaluator)


def execution_dependencies(store, task_id, evaluator=None, coder=None, local=None):
    """Repair and delivery workers select the same adapters and frozen interpreter."""
    if evaluator is None:
        from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
        evaluator = ServiceEvaluator()
    if coder is None:
        from chatcopilot.harness.codex_adapter import CodexCoder
        coder = CodexCoder(lambda root, ref: store.register_trace(task_id, root, ref))
    local = local or LocalVerifier(store.root)
    task = store.get(task_id)
    if task.get("environment", {}).get("python"):
        local.python = task["environment"]["python"]
    if task["source"].get("kind") == "code_health":
        from chatcopilot.harness.governance_verification import GovernanceVerification
        verifier = GovernanceVerification(evaluator, local, store)
    elif task["source"].get("kind") == "application":
        from chatcopilot.harness.improvement_verification import ImprovementVerification
        verifier = ImprovementVerification(evaluator, local, store)
    else:
        from chatcopilot.harness.verification import CaseVerification
        verifier = CaseVerification(evaluator, local, store)
    return coder, local, verifier


def run_task(store, task_id, evaluator=None, coder=None, *, local_verifier=None, committer=None):
    from chatcopilot.harness.delivery import initialize, reconcile
    from chatcopilot.harness.models import PIPELINE_VERSION
    if store.get(task_id).get("pipeline_version") != PIPELINE_VERSION:
        return store.get(task_id)
    governance = store.get(task_id)["source"].get("kind") == "code_health"
    coder, local, verifier = execution_dependencies(store, task_id, evaluator, coder, local_verifier)
    def prepare(budget):
        from pathlib import Path
        from chatcopilot.harness.artifact_repository import ArtifactRepository
        from chatcopilot.harness.task_environment import prepare_environment
        from dataclasses import asdict
        budget.check()
        initialize(store, task_id)
        if store.get(task_id)["source"].get("kind") == "application":
            from chatcopilot.harness.improvement_repository import prepare_upstream
            prepare_upstream(store, task_id, budget.check)
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
            previous_run_findings = []
            for row in prior:
                if (row.get("governance_run_id") != current.get("governance_run_id")
                        or row.get("skill_learning_origin") or not row.get("frozen_finding")):
                    continue
                finding = ArtifactRepository(store.root / "jobs" / row["task_id"]).read(row["frozen_finding"])
                previous_run_findings.append({"id": finding["id"], "summary": finding["summary"],
                    "principle_refs": finding["principle_refs"], "affected_paths": finding["affected_paths"],
                    "evidence": [{key: evidence[key] for key in ("path", "start_line", "end_line")}
                                 for evidence in finding["evidence"]]})
            store.update(task_id, governance_context=asdict(context), source={**current["source"],
                         "governance_context": asdict(context), "previous_governance": recent,
                         "previous_run_findings": previous_run_findings})
        if governance and not store.get(task_id).get("harness_skill"):
            from chatcopilot.harness.skill_context import freeze_skill
            reference = freeze_skill(artifacts, frozen)
            if reference:
                store.update(task_id, harness_skill=reference)
        store.update(task_id, stage="environment")
        environment = prepare_environment(artifacts.directory, artifacts.directory / "source", budget.check)
        store.update(task_id, environment=environment)
        local.python = environment["python"]
        budget.check()

    return execute(store, task_id, verifier, coder, prepare=prepare,
                   deliver=lambda: reconcile(store, task_id, coder=coder, verifier=verifier))
