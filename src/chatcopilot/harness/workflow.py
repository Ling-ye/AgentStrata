"""Drive the repair graph; dependencies are supplied by the runtime composition."""
import sqlite3
from chatcopilot.core.private_sqlite import storage_error_details
from chatcopilot.harness.config import safe_error
from chatcopilot.harness.graph_repository import checkpoints
from chatcopilot.harness.graph_service import execute_graph
from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION
from chatcopilot.harness.engine.repair_graph import build_task_graph
from chatcopilot.harness.engine.context import HarnessContext


def run_task(context: HarnessContext):
    store, task_id = context.store, context.task_id
    task = store.get(task_id)
    if task.get("pipeline_version") != PIPELINE_VERSION:
        raise HarnessError("source_archived", "旧任务仅供归档读取，请创建新任务")
    if task["status"] not in {"queued", "running", "cancel_requested"}:
        return task
    identity = "task:" + task_id
    with context.budget, checkpoints(store.root, identity) as saver:
        store.update(task_id, if_status=frozenset({"queued"}), status="running")
        graph = build_task_graph(saver)
        saved = graph.get_state({"configurable": {"thread_id": identity}})
        if (saved.next or saved.tasks) and saved.values.get("attempt"):
            store.update(task_id, current_attempt=saved.values["attempt"])

        def project(snapshot):
            store.update(task_id, graph={"thread_id": identity, "checkpoint_id": snapshot.config.get(
                "configurable", {}).get("checkpoint_id"), "next": list(snapshot.next),
                "phase": snapshot.values.get("phase"), "version": snapshot.values.get("version")})

        try:
            complete = not (saved.next or saved.tasks) and saved.values.get("outcome") == "fixed"
            value = execute_graph(graph, identity, initial=None if complete else {
                "task_id": task_id, "pipeline_version": PIPELINE_VERSION}, observe=project,
                context=context, step_limit=max(100, context.options.max_attempts * 40 + 20))
            if value.get("outcome") == "fixed":
                context.recover_accepted()
        except HarnessError as exc:
            context.finish("blocked", exc.code, safe_error(exc))
        except Exception as exc:
            if isinstance(exc, sqlite3.Error) or storage_error_details(exc):
                raise
            context.finish("interrupted", "execution_error", safe_error(exc))
    return store.get(task_id)
