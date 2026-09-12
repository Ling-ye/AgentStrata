"""One optional worker process, independent of Console and Evaluation supervision."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
from pathlib import Path

from chatcopilot.core.private_sqlite import private_directory, private_file
from chatcopilot.harness.codex_adapter import CodexCoder
from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
from chatcopilot.harness.models import ACTIVE
from chatcopilot.harness.store import HarnessStore
from chatcopilot.harness.assembly import run_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    store = HarnessStore(args.root)
    task = store.get(args.task)
    if task["status"] not in ACTIVE:
        return 0
    directory = private_directory(args.root / "jobs" / task["task_id"])
    fd = os.open(directory / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_file(directory / "worker.lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, ValueError):
        os.close(fd)
        return 2

    def cancel(_signum, _frame):
        store.update(args.task, status="cancel_requested")

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        result = run_task(store, args.task, ServiceEvaluator(), CodexCoder())
        return 0 if result["status"] in {"fixed", "not_reproduced", "cancelled"} else 1
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
