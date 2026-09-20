"""Explicit destructive protocol cutover under the existing maintenance leases."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from chatcopilot.core.private_sqlite import private_file
from chatcopilot.core.source_snapshot import git_output
from chatcopilot.harness.control_types import EVALUATION_TERMINAL_STATES, WorkerState, external_evaluation_id
from chatcopilot.harness.github_delivery import git
from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION

_TASK = re.compile(r"repair-[0-9a-f]{32}")


def _inventory(store):
    with store.database.connect() as connection:
        ids = [row[0] for row in connection.execute("SELECT task_id FROM tasks")]
    tasks = {ident: store.get(ident) for ident in ids}
    current = set(tasks)
    archives = store.root / "archives"
    if archives.exists():
        if archives.is_symlink():
            raise HarnessError("cutover_path_unsafe", "归档目录不能是符号链接")
        for database in archives.glob("*/harness.sqlite3"):
            if database.resolve() != database:
                raise HarnessError("cutover_path_unsafe", "归档数据库路径不安全")
            private_file(database)
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                for (raw,) in connection.execute("SELECT payload FROM tasks"):
                    task = json.loads(raw)
                    tasks.setdefault(task["task_id"], task)
    jobs = store.root / "jobs"
    if jobs.is_symlink():
        raise HarnessError("cutover_path_unsafe", "任务目录不能是符号链接")
    for folder in jobs.iterdir():
        if not _TASK.fullmatch(folder.name) or folder.resolve() != folder or not folder.is_dir():
            raise HarnessError("cutover_path_unsafe", "存在无法确认归属的任务目录")
        tasks.setdefault(folder.name, {"task_id": folder.name, "source": {}})
    for task in tasks.values():
        ident = task["task_id"]
        if not _TASK.fullmatch(ident):
            raise HarnessError("cutover_path_unsafe", "任务身份无效")
        task.setdefault("unit", "agentstrata-harness-" + ident[7:])
    return tasks, current


def cutover(store, workers, evaluator, *, apply=False, github=None, repository=None):
    with store.maintenance():
        tasks, current = _inventory(store)
        if any(tasks[ident].get("pipeline_version") == PIPELINE_VERSION for ident in current):
            raise HarnessError("cutover_current_tasks", "新协议已创建任务，不允许用旧协议切换命令清空")
        for task in tasks.values():
            if workers.observe(task) != WorkerState.INACTIVE:
                raise HarnessError("cutover_worker_active", "无法确认旧 worker 已停止：" + task["task_id"])
            references = [external_evaluation_id(task.get("source", {}), task.get("current_evaluation_id")),
                          external_evaluation_id(task.get("source", {}), (task.get("delivery_evaluation") or {}).get("id"))]
            for ident in filter(None, references):
                if evaluator.execution_status(ident) not in EVALUATION_TERMINAL_STATES | {"failed"}:
                    raise HarnessError("cutover_evaluation_active", "旧任务外部测评尚未结束")
            delivery = task.get("delivery") or {}
            if delivery.get("pr_number"):
                if github is None:
                    raise HarnessError("cutover_delivery_unknown", "需要 GitHub 对账确认旧 PR 已结束")
                pr = github.pull(delivery["pr_number"])
                github.verify_pr(pr, delivery)
                if pr.get("state") != "closed":
                    raise HarnessError("cutover_delivery_active",
                        f"旧 PR 尚未关闭或合并：{delivery.get('repository', '')}#{delivery['pr_number']}；不自动关闭人工工作")
            elif delivery.get("commit_sha") and delivery.get("state") not in {"merged", "closed", "cancelled"}:
                raise HarnessError("cutover_delivery_unknown", "存在未完成交付的提交，先完成交付对账")
        roots = {Path(task["repository"]) for task in tasks.values() if task.get("repository")}
        if repository is not None:
            roots.add(Path(repository))
        if len(roots) > 1:
            raise HarnessError("cutover_repository_mismatch", "旧记录涉及不同仓库，不能统一清空")
        repo = next(iter(roots), None)
        worktrees, branches = [], []
        if repo:
            records = git_output(repo, "worktree", "list", "--porcelain").split("\n\n")
            for record in records:
                fields = dict(line.split(" ", 1) for line in record.splitlines() if " " in line)
                if "worktree" not in fields:
                    continue
                path = Path(fields["worktree"])
                branch = fields.get("branch", "").removeprefix("refs/heads/")
                owned = next((ident for ident in tasks if path == store.root / "jobs" / ident / "worktree"), None)
                managed_branch = next((ident for ident in tasks if branch == "feat/harness-" + ident[7:]), None)
                if managed_branch and owned != managed_branch:
                    raise HarnessError("cutover_workspace_changed", "任务分支正在其他工作区使用")
                if owned:
                    if path.exists() and path.resolve() != path:
                        raise HarnessError("cutover_workspace_changed", "任务工作区路径发生变化")
                    if branch != "feat/harness-" + owned[7:]:
                        raise HarnessError("cutover_workspace_changed", "任务工作区分支归属已变化")
                    task = tasks[owned]
                    expected = {task.get("base_commit"), task.get("working_head"),
                                (task.get("delivery") or {}).get("commit_sha"),
                                (task.get("archive") or {}).get("head"), (task.get("local_commit") or {}).get("sha")}
                    expected.discard(None)
                    if expected and fields.get("HEAD") not in expected:
                        raise HarnessError("cutover_workspace_changed", "任务工作区 HEAD 已发生未记录的变化")
                    worktrees.append(str(path))
            for branch in git_output(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/feat/harness-*").splitlines():
                if "repair-" + branch.removeprefix("feat/harness-") in tasks:
                    branches.append(branch)
        result = {"tasks": len(current), "task_directories": len(list((store.root / "jobs").iterdir())),
                  "task_ids": sorted(tasks), "worktrees": worktrees, "branches": branches,
                  "target_pipeline": PIPELINE_VERSION, "applied": False}
        if not apply:
            return result
        lease = uuid.uuid4().hex
        evaluator.client.enter_maintenance(lease)
        try:
            if repo:
                for path in worktrees:
                    git(repo, "worktree", "remove", "--force", path)
                for branch in branches:
                    git(repo, "branch", "-D", branch)
            for ident in tasks:
                path = store.root / "jobs" / ident
                if path.exists():
                    shutil.rmtree(path)
            archive = store.root / "archives"
            if archive.exists():
                shutil.rmtree(archive)
            with store.database.connect(write=True) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for name in ("repair_steps", "attempts", "governance_runs", "tasks"):
                    if name in tables:
                        connection.execute("DELETE FROM " + name)
                connection.execute("DROP TRIGGER IF EXISTS harness_pipeline_admission")
                connection.execute(f"CREATE TRIGGER harness_pipeline_admission BEFORE INSERT ON tasks WHEN COALESCE(json_extract(NEW.payload, '$.pipeline_version'), 0) != {PIPELINE_VERSION} BEGIN SELECT RAISE(ABORT, 'Harness protocol changed; restart the upgraded host'); END")
            return {**result, "applied": True}
        finally:
            evaluator.client.leave_maintenance(lease)
