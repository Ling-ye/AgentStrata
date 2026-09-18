"""One-time archive/reset, guarded by the existing host maintenance protocol."""
from __future__ import annotations

import hashlib
from pathlib import Path
import time
import uuid

from chatcopilot.core.private_sqlite import PrivateDatabase, json_text, private_directory
from chatcopilot.core.source_snapshot import copy_sources, source_manifest
from chatcopilot.harness.control_types import WorkerState, external_evaluation_id
from chatcopilot.harness.github_delivery import git
from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION


def cutover(store, workers, evaluator, *, apply=False, github=None):
    with store.maintenance():
        with store.database.connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT task_id FROM tasks ORDER BY created_at")]
        tasks = [store.get(ident) for ident in ids]
        if any(task.get("pipeline_version") == PIPELINE_VERSION for task in tasks):
            raise HarnessError("cutover_current_tasks", "新协议已创建任务，不允许用旧协议切换命令清空")
        for task in tasks:
            if workers.observe(task) != WorkerState.INACTIVE:
                raise HarnessError("cutover_worker_active", "无法确认旧 worker 已停止：" + task["task_id"])
            references = [external_evaluation_id(task["source"], task.get("current_evaluation_id")),
                          external_evaluation_id(task["source"], (task.get("delivery_evaluation") or {}).get("id"))]
            for ident in filter(None, references):
                if evaluator.execution_status(ident) not in {"completed", "failed", "cancelled"}:
                    raise HarnessError("cutover_evaluation_active", "旧任务外部测评尚未结束")
            delivery = task.get("delivery") or {}
            if delivery.get("pr_number"):
                if github is None:
                    raise HarnessError("cutover_delivery_unknown", "需要 GitHub 对账确认旧 PR 已结束")
                pr = github.pull(delivery["pr_number"])
                github.verify_pr(pr, delivery)
                if pr.get("state") != "closed" or pr.get("auto_merge"):
                    raise HarnessError("cutover_delivery_active", "旧 PR 尚未关闭或合并，不自动关闭人工工作")
            elif delivery.get("commit_sha"):
                raise HarnessError("cutover_delivery_unknown", "存在未完成交付的提交，先完成交付对账")
        result = {"tasks": len(tasks), "task_ids": ids, "target_pipeline": PIPELINE_VERSION,
                  "applied": False, "retained_jobs": str(store.root / "jobs")}
        if not apply or not tasks:
            return result
        lease = uuid.uuid4().hex
        evaluator.client.enter_maintenance(lease)
        try:
            archive = private_directory(store.root / "archives" / (str(int(time.time())) + "-" + uuid.uuid4().hex[:8]))
            backup = PrivateDatabase(archive / "harness.sqlite3", "")
            with store.database.connect() as source, backup.connect() as destination:
                destination.rollback()
                source.backup(destination)
            workspaces = []
            for task in tasks:
                path = Path(task["worktree"]) if task.get("worktree") else None
                if path is None or not path.exists():
                    continue
                expected = store.root / "jobs" / task["task_id"] / "worktree"
                if path != expected or path.resolve() != path:
                    raise HarnessError("cutover_workspace_changed", "旧任务工作区归属不符，保留现场")
                manifest = source_manifest(path)
                folder = private_directory(archive / task["task_id"])
                copy_sources(path, folder / "source", manifest)
                bundle = folder / "recovery.bundle"
                git(path, "bundle", "create", str(bundle), "HEAD")
                bundle.chmod(0o600)
                git(path, "bundle", "verify", str(bundle))
                if source_manifest(path) != manifest:
                    raise HarnessError("cutover_workspace_changed", "归档期间旧工作区变化")
                workspaces.append({"task_id": task["task_id"], "source": manifest,
                                   "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()})
            receipt = {**result, "workspaces": workspaces, "database_sha256": hashlib.sha256((archive / "harness.sqlite3").read_bytes()).hexdigest()}
            (archive / "manifest.json").write_text(json_text(receipt))
            (archive / "manifest.json").chmod(0o600)
            # Historical evidence and original Git worktrees stay at their existing
            # paths. Only the active index is reset, after the recoverable backup.
            with store.database.connect(write=True) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS repair_steps (task_id TEXT NOT NULL REFERENCES tasks(task_id), step_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(task_id, step_id))")
                connection.execute("DELETE FROM repair_steps")
                connection.execute("DELETE FROM attempts")
                connection.execute("DELETE FROM tasks")
                connection.execute(f"CREATE TRIGGER IF NOT EXISTS harness_pipeline_admission BEFORE INSERT ON tasks WHEN COALESCE(json_extract(NEW.payload, '$.pipeline_version'), 0) != {PIPELINE_VERSION} BEGIN SELECT RAISE(ABORT, 'Harness protocol changed; restart the upgraded host'); END")
            return {**result, "applied": True, "archive": str(archive)}
        finally:
            evaluator.client.leave_maintenance(lease)
