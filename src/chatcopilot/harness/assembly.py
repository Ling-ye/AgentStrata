"""Worker composition; orchestration itself knows no concrete execution backends."""
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.verification import CaseVerification
from chatcopilot.harness.workflow import run_task as execute


def run_task(store, task_id, evaluator, coder, *, local_verifier=None, committer=None):
    from chatcopilot.harness.delivery import initialize, reconcile
    from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION, safe_error
    if store.get(task_id).get("pipeline_version") != PIPELINE_VERSION:
        return store.get(task_id)
    verifier = CaseVerification(evaluator, local_verifier or LocalVerifier(store.root), store)
    try:
        initialize(store, task_id)
        if store.get(task_id)["source"].get("kind") == "code_health":
            from chatcopilot.harness.code_health import run_task as maintain
            maintain(store, task_id, coder)
        else:
            execute(store, task_id, verifier, coder)
    except HarnessError as exc:
        store.update(task_id, status="blocked", stage="done", error_code=exc.code, message=safe_error(exc))
    return reconcile(store, task_id, coder=coder, verifier=verifier)
