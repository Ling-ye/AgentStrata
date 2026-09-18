"""Worker composition; orchestration itself knows no concrete execution backends."""
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.verification import CaseVerification
from chatcopilot.harness.repair_runtime import run_task as execute


def run_task(store, task_id, evaluator, coder, *, local_verifier=None, committer=None):
    from chatcopilot.harness.delivery import initialize, reconcile
    from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION
    from chatcopilot.harness.config import safe_error
    if store.get(task_id).get("pipeline_version") != PIPELINE_VERSION:
        return store.get(task_id)
    local = local_verifier or LocalVerifier(store.root)
    verifier = CaseVerification(evaluator, local, store)
    try:
        initialize(store, task_id)
        from dataclasses import asdict
        from pathlib import Path
        from chatcopilot.harness.artifact_repository import ArtifactRepository
        artifacts = ArtifactRepository(store.root / "jobs" / task_id)
        if not store.get(task_id).get("principles"):
            store.update(task_id, principles=asdict(artifacts.principles(Path(__file__).resolve().parents[3])))
        from chatcopilot.harness.task_environment import prepare_environment
        from chatcopilot.harness.control_service import check_cancellation
        import time
        task = store.get(task_id)
        store.update(task_id, stage="environment")
        started = time.monotonic()
        elapsed = task.get("elapsed_seconds", 0)
        def poll():
            check_cancellation(store.control_state(task_id))
            if elapsed + time.monotonic() - started >= task["options"]["timeout_seconds"]:
                raise HarnessError("budget_exhausted", "任务依赖准备已用完累计预算")
        try:
            environment = prepare_environment(artifacts.directory, artifacts.directory / "source", poll)
            store.update(task_id, environment=environment)
            local.python = environment["python"]
        finally:
            store.update(task_id, elapsed_seconds=elapsed + time.monotonic() - started)
        execute(store, task_id, verifier, coder)
    except HarnessError as exc:
        store.update(task_id, status="blocked", stage="done", error_code=exc.code, message=safe_error(exc))
    return reconcile(store, task_id, coder=coder, verifier=verifier)
