"""Execute the persistent task graph using injected host operations."""
import sqlite3

from chatcopilot.core.private_sqlite import storage_error_details
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.graph_repository import checkpoints
from chatcopilot.harness.graph_service import execute_graph
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.task_graph import build_task_graph
from chatcopilot.harness.task_operations import TaskOperations


def run_task(store, task_id, verifier, coder, *, workspace_factory, prepare=None, deliver=None):
    task = store.get(task_id)
    if task["status"] not in {"queued", "running", "cancel_requested"}:
        return task
    operations = TaskOperations(store, task_id, verifier, coder, workspace_factory, prepare=prepare, deliver=deliver)
    identity = "task:" + task_id
    with operations.budget, checkpoints(store.root, identity) as saver:
        store.update(task_id, if_status=frozenset({"queued"}), status="running")
        graph = build_task_graph(operations, saver)
        saved = graph.get_state({"configurable": {"thread_id": identity}})
        if (saved.next or saved.tasks) and saved.values.get("attempt"):
            store.update(task_id, current_attempt=saved.values["attempt"])

        def project(snapshot):
            store.update(task_id, graph={"thread_id": identity, "checkpoint_id": snapshot.config.get(
                "configurable", {}).get("checkpoint_id"), "next": list(snapshot.next),
                "phase": snapshot.values.get("phase"), "version": snapshot.values.get("version")})

        try:
            complete = not (saved.next or saved.tasks) and saved.values.get("outcome") == "fixed"
            value = execute_graph(graph, identity, initial=None if complete else {}, observe=project,
                                  step_limit=max(100, operations.options.max_attempts * 40 + 20))
            if value.get("outcome") == "fixed":
                operations.recover_accepted()
        except HarnessError as exc:
            operations.finish("blocked", exc.code, safe_error(exc))
        except Exception as exc:
            if isinstance(exc, sqlite3.Error) or storage_error_details(exc):
                raise
            operations.finish("interrupted", "execution_error", safe_error(exc))
    return store.get(task_id)
