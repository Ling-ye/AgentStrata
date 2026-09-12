"""Worker composition; orchestration itself knows no concrete execution backends."""
from chatcopilot.harness.local_commit import LocalCommitter
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.verification import CaseVerification
from chatcopilot.harness.workflow import run_task as execute


def run_task(store, task_id, evaluator, coder, *, local_verifier=None, committer=None):
    return execute(store, task_id, CaseVerification(evaluator, local_verifier or LocalVerifier(store.root), store),
                   coder, committer=committer or LocalCommitter())
