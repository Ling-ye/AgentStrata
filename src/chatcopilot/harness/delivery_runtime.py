"""Short-lived delivery workers and read-only selection for the systemd timer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from chatcopilot.harness.config import configuration
from chatcopilot.harness.delivery import PENDING, reconcile
from chatcopilot.harness.models import ACTIVE, HarnessError, PIPELINE_VERSION
from chatcopilot.harness.control_service import HarnessLifecycle
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.worker_runtime import SystemdWorkerControl, worker_execution
from chatcopilot.harness.store import HarnessStore


def launch_delivery(store: HarnessStore, task_id: str) -> None:
    task = store.get(task_id)
    if task.get("pipeline_version") != PIPELINE_VERSION or not task.get("delivery"):
        raise HarnessError("source_archived", "旧任务只读保留")
    settings = configuration()
    workers = SystemdWorkerControl(Path(__file__).resolve().parents[3], store.root, settings)
    with store.control_guard(task_id):
        task = store.get(task_id)
        if task["status"] in {*ACTIVE, "waiting_input"}:
            return
        if workers.observe(task) != WorkerState.INACTIVE:
            return
        result = workers.launch_delivery(task)
        if result.state == "failed":
            raise HarnessError(result.code, result.message)


def pending(store: HarnessStore) -> list[str]:
    with store.database.connect() as connection:
        rows = connection.execute("SELECT payload FROM tasks WHERE json_extract(payload, '$.pipeline_version')=?", (PIPELINE_VERSION,)).fetchall()
    result = []
    for row in rows:
        task = json.loads(row[0])
        state = task.get("delivery", {}).get("state")
        if task["status"] not in {*ACTIVE, "waiting_input"} and task.get("delivery") and (
            state in PENDING or task.get("delivery_request") or task.get("cleanup", {}).get("local") == "pending"
            or (state in {"merged", "closed"} and task.get("cleanup", {}).get("remote") != "cleaned")
        ):
            result.append(task["task_id"])
    return result


def run_one(store: HarnessStore, task_id: str) -> dict[str, Any]:
    from chatcopilot.harness.codex_adapter import CodexCoder
    from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
    from chatcopilot.harness.local_verifier import LocalVerifier
    from chatcopilot.harness.verification import CaseVerification
    try:
        with worker_execution(store, task_id, delivery=True) as task:
            if task is None:
                return store.get(task_id)
            action = task.get("delivery_request")
            coder = CodexCoder(lambda root, ref: store.register_trace(task_id, root, ref))
            verifier = CaseVerification(ServiceEvaluator(), LocalVerifier(store.root), store)
            result = reconcile(store, task_id, coder=coder, verifier=verifier,
                               retry=action == "retry" or task["delivery"]["state"] in {"merged", "closed"},
                               cleanup_only=action == "cleanup" or (not action and task["delivery"]["state"] not in PENDING
                                   and task.get("cleanup", {}).get("local") == "pending"))
            cancellation_pending = (result.get("delivery_evaluation") or
                result["delivery"]["state"] not in {"cancelled", "merged", "closed"})
            if store.get(task_id).get("delivery_request") == action and not (action == "cancel" and cancellation_pending):
                store.update(task_id, delivery_request=None)
            return result
    except HarnessError as error:
        if error.code == "worker_active":
            return store.get(task_id)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--task")
    args = parser.parse_args(argv)
    os.umask(0o077)
    from chatcopilot.harness.config import default_root
    store = HarnessStore(args.root or default_root(args.repository_root))
    if args.task:
        run_one(store, args.task)
    else:
        from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
        settings = configuration()
        socket_path = settings.get("CHATCOPILOT_EVALUATION_SOCKET")
        lifecycle = HarnessLifecycle(store, SystemdWorkerControl(args.repository_root, store.root, settings),
            ServiceEvaluator(socket_path=Path(socket_path) if socket_path else None))
        lifecycle.reconcile_active()
        for ident in pending(store):
            try:
                launch_delivery(store, ident)
            except HarnessError:
                # Leave durable work pending; the next timer tick can dispatch again.
                import logging
                logging.getLogger(__name__).warning("Harness delivery dispatch unavailable for %s", ident)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
