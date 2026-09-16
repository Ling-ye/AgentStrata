"""Short-lived delivery workers and read-only selection for the systemd timer."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from chatcopilot.core.private_sqlite import private_file
from chatcopilot.harness.config import configuration
from chatcopilot.harness.delivery import PENDING, reconcile
from chatcopilot.harness.models import ACTIVE, HarnessError, PIPELINE_VERSION
from chatcopilot.harness.store import HarnessStore


def launch_delivery(store: HarnessStore, task_id: str) -> None:
    task = store.get(task_id)
    if task.get("pipeline_version") != PIPELINE_VERSION or not task.get("delivery"):
        raise HarnessError("source_archived", "旧任务只读保留")
    runtime = store.root / "jobs" / task_id / "runtime"
    if not runtime.exists():
        # A pre-launch failure has no running host; freeze the current trusted host for cleanup.
        from chatcopilot.core.source_snapshot import copy_sources, source_manifest
        host = Path(__file__).resolve().parents[3]
        copy_sources(host, runtime, source_manifest(host))
    if runtime.resolve() != runtime or not (runtime / "src/chatcopilot/harness/delivery_runtime.py").is_file():
        raise HarnessError("delivery_runtime_missing", "冻结的交付宿主不可用")
    unit = "agentstrata-harness-delivery-" + task_id[7:]
    command = ["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
               "--property=Type=exec", "--property=KillMode=control-group", "--property=UMask=0077",
               "--property=WorkingDirectory=" + str(runtime), "--setenv=PYTHONPATH=" + str(runtime / "src")]
    settings = configuration()
    for key, value in settings.items():
        command.append("--setenv=" + key + "=" + value)
    command += [sys.executable, "-m", "chatcopilot.harness.delivery_runtime", "--root", str(store.root), "--task", task_id]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        active = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, timeout=10)
        if active.returncode:
            raise HarnessError("delivery_worker_unavailable", "交付后台任务未启动，请检查 systemd 用户服务")


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
    from chatcopilot.harness.delivery_archive import task_paths
    task = store.get(task_id)
    _, directory, _, _ = task_paths(store, task)
    path = directory / "worker.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_file(path)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, ValueError):
        os.close(fd)
        return store.get(task_id)
    try:
        with store.creation_guard():
            task = store.get(task_id)
            action = task.get("delivery_request")
            coder = CodexCoder(lambda root, ref: store.register_trace(task_id, root, ref))
            verifier = CaseVerification(ServiceEvaluator(), LocalVerifier(store.root), store)
            result = reconcile(store, task_id, coder=coder, verifier=verifier,
                               retry=action == "retry" or task["delivery"]["state"] in {"merged", "closed"},
                               cleanup_only=action == "cleanup" or (not action and task["delivery"]["state"] not in PENDING
                                   and task.get("cleanup", {}).get("local") == "pending"))
            if store.get(task_id).get("delivery_request") == action:
                store.update(task_id, delivery_request=None)
            return result
    finally:
        os.close(fd)


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
