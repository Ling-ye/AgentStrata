"""One optional worker process, independent of Console and Evaluation supervision."""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
from pathlib import Path

from chatcopilot.core.private_sqlite import storage_error_details
from chatcopilot.harness.codex_adapter import CodexCoder
from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
from chatcopilot.harness.models import ACTIVE, HarnessError
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.assembly import run_task
from chatcopilot.harness.worker_runtime import worker_execution


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    store = HarnessStore(args.root)
    def cancel(_signum, _frame):
        current = store.get(args.task)
        store.update(args.task, delivery_cancel_requested=True,
                     **({"status": "cancel_requested"} if current["status"] in ACTIVE else {}))

    try:
        with worker_execution(store, args.task) as task:
            if task is None:
                return 0
            signal.signal(signal.SIGTERM, cancel)
            signal.signal(signal.SIGINT, cancel)
            try:
                result = run_task(store, args.task, ServiceEvaluator(), CodexCoder(
                    lambda root, ref: store.register_trace(args.task, root, ref)))
                return 0 if result["status"] in {"fixed", "not_reproduced", "cancelled"} else 1
            except (sqlite3.Error, OSError) as error:
                if not isinstance(error, sqlite3.Error) and not storage_error_details(error):
                    raise
                store.interrupt(args.task, storage_error=error)
                return 1
    except HarnessError as error:
        if error.code == "worker_active":
            return 2
        raise



if __name__ == "__main__":
    raise SystemExit(main())
