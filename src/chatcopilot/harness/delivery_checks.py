"""Public-boundary gates on exactly the history that will be pushed."""
from __future__ import annotations

from pathlib import Path
import signal
import os
import subprocess
import sys
import time
import uuid
from typing import Any, Callable

from chatcopilot.core.github_transport import git_environment
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.harness.github_delivery import git
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.config import safe_error


def publication_checks(store: Any, task_id: str, check_cancel: Callable[[], None]) -> None:
    task = store.get(task_id)
    state = task["delivery"]
    if state.get("public_checks", {}).get("sha") == state["commit_sha"]:
        return
    folder = private_directory(store.root / "jobs" / task_id / "publication-checks" / uuid.uuid4().hex)
    view = folder / "source"
    env = git_environment()
    # Single-branch clone excludes unrelated private/operator branches from the history gate.
    git(Path(task["repository"]), "clone", "--quiet", "--no-local", "--single-branch", "--branch", task["branch"],
        str(task["repository"]), str(view), env=env)
    if git(view, "rev-parse", "HEAD").decode().strip() != state["commit_sha"]:
        raise HarnessError("workspace_changed", "公开检查克隆与验收提交不一致")
    policy = Path(__file__).resolve().parents[3]
    commands = [("public", [sys.executable, str(policy / "scripts/check_public_repo.py"), "--root", str(view), "--history"]),
                ("secrets", ["bash", str(policy / "scripts/check_secrets.sh"), "history"])]
    checks = []
    for name, argv in commands:
        check_cancel()
        log = folder / (name + ".log")
        with log.open("wb") as stream:
            process = subprocess.Popen(argv, cwd=view, env={**env, "AGENTSTRATA_REPO_ROOT": str(view)},
                                       stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while process.poll() is None:
                    check_cancel()
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()

        log.write_text(safe_error(Exception(log.read_text(errors="replace"))), encoding="utf-8")
        log.chmod(0o600)
        checks.append({"name": name, "exit_code": process.returncode, "log": log.relative_to(store.root / "jobs" / task_id).as_posix()})
        if process.returncode:
            raise HarnessError("publication_check_failed", "公开交付检查未通过：" + name)
    if git(view, "rev-parse", "HEAD").decode().strip() != state["commit_sha"]:
        raise HarnessError("workspace_changed", "公开检查期间提交变化")
    current = store.get(task_id)
    store.update(task_id, delivery={**current["delivery"], "public_checks": {"sha": state["commit_sha"], "checks": checks}})
