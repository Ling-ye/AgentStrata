"""Harness owns only tasks and attempts; evaluation results remain external."""

from __future__ import annotations

import json
import time
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import PrivateDatabase, json_text, private_file
from chatcopilot.harness.models import ACTIVE, Cancelled, HarnessError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, match_key TEXT NOT NULL,
 context_key TEXT NOT NULL, active_key TEXT UNIQUE, status TEXT NOT NULL,
 payload TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_history ON tasks(context_key, updated_at DESC);
CREATE TABLE IF NOT EXISTS attempts (
 task_id TEXT NOT NULL REFERENCES tasks(task_id), number INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(task_id, number)
);
"""


class HarnessStore:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.database = PrivateDatabase(self.root / "harness.sqlite3", _SCHEMA)

    @contextmanager
    def creation_guard(self, *, exclusive: bool = False):
        """Share the existing database inode lock with the update process."""
        import fcntl
        path = self.root / "harness.sqlite3"
        before = private_file(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            current = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise HarnessError("maintenance_unknown", "Harness 数据库身份发生变化")
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HarnessError("maintenance_active", "Harness 正在维护或接受另一项操作，请稍后重试") from exc
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def maintenance(self):
        """Atomically prove idle and prevent new/resumed work through the update."""
        with self.creation_guard(exclusive=True) as descriptor:
            with self.database.connect() as connection:
                placeholders = ",".join("?" for _ in ACTIVE)
                if connection.execute(f"SELECT 1 FROM tasks WHERE status IN ({placeholders}) LIMIT 1", tuple(ACTIVE)).fetchone():
                    raise HarnessError("maintenance_blocked", "Harness 有活动任务，不能更新运行代码")
            yield descriptor

    def by_request(self, request_key: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE request_key=?", (request_key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, task: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = time.time()
        with self.creation_guard(), self.database.connect(write=True) as connection:
            old = connection.execute(
                "SELECT payload FROM tasks WHERE request_key=? OR active_key=?",
                (task["request_key"], task["active_key"]),
            ).fetchone()
            if old:
                value = json.loads(old[0])
                if (
                    value["request_key"] == task["request_key"]
                    and value["request_digest"] != task["request_digest"]
                ):
                    raise HarnessError("conflict", "同一请求 ID 的内容已变化")
                return value, False
            task = {
                **task,
                "status": "queued",
                "stage": "queued",
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task["task_id"],
                    task["request_key"],
                    task["match_key"],
                    task["context_key"],
                    task["active_key"],
                    "queued",
                    json_text(task),
                    now,
                    now,
                ),
            )
        return task, True

    def get(self, task_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise HarnessError("not_found", "修复任务不存在")
        return json.loads(row[0])

    def update(
        self, task_id: str, *, if_status: frozenset[str] | None = None, **changes: Any
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise HarnessError("not_found", "修复任务不存在")
            current = json.loads(row[0])
            if if_status is not None and current["status"] not in if_status:
                return current
            if current["status"] == "cancel_requested" and changes.get("status") in {
                "running",
                "fixed",
            }:
                raise Cancelled()
            value = {**current, **changes, "updated_at": time.time()}
            active_key = (
                value["active_key"]
                if value["status"] in ACTIVE or value.get("current_evaluation_id")
                else None
            )
            connection.execute(
                "UPDATE tasks SET status=?,active_key=?,payload=?,updated_at=? WHERE task_id=?",
                (value["status"], active_key, json_text(value), value["updated_at"], task_id),
            )
        return value

    def save_attempt(self, task_id: str, number: int, payload: dict[str, Any]) -> None:
        with self.database.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO attempts VALUES(?,?,?) ON CONFLICT(task_id,number) DO UPDATE SET payload=excluded.payload",
                (task_id, number, json_text(payload)),
            )

    def attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM attempts WHERE task_id=? ORDER BY number", (task_id,)
                )
            ]

    def claim_resume(self, task_id: str) -> tuple[dict[str, Any], bool]:
        with self.creation_guard(), self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise HarnessError("not_found", "修复任务不存在")
            task = json.loads(row[0])
            if task["status"] not in {"blocked", "interrupted", "cancelled"}:
                return task, False
            task.update(
                status="queued",
                dispatch_state="creating",
                message="准备继续",
                updated_at=time.time(),
            )
            connection.execute(
                "UPDATE tasks SET status='queued',active_key=?,payload=?,updated_at=? WHERE task_id=?",
                (task["active_key"], json_text(task), task["updated_at"], task_id),
            )
            return task, True

    def finish_verified(self, task_id: str, number: int, attempt: dict[str, Any]) -> None:
        if (
            attempt.get("status") != "accepted"
            or not attempt.get("candidate_digest")
            or attempt.get("regressions")
        ):
            raise ValueError("a complete accepted verification is required")
        with self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise HarnessError("not_found", "修复任务不存在")
            task = json.loads(row[0])
            if task["status"] != "running":
                raise Cancelled()
            if task.get("review_and_commit") and (
                attempt.get("review", {}).get("decision") != "approved"
                or not task.get("local_commit")
            ):
                raise ValueError("review and local commit receipts are required")
            task.update(
                status="fixed",
                stage="done",
                verified_at=time.time(),
                verified_digest=attempt.get("delivered_digest", attempt["candidate_digest"]),
                verification_evaluation_id=attempt["verification"]["evaluation_id"],
                updated_at=time.time(),
                message="AI 审核与验证通过，已创建本地提交"
                if task.get("local_commit")
                else "worktree 验收通过；改动尚未提交或合入",
            )
            connection.execute(
                "INSERT INTO attempts VALUES(?,?,?) ON CONFLICT(task_id,number) DO UPDATE SET payload=excluded.payload",
                (task_id, number, json_text(attempt)),
            )
            connection.execute(
                "UPDATE tasks SET status='fixed',active_key=NULL,payload=?,updated_at=? WHERE task_id=?",
                (json_text(task), task["updated_at"], task_id),
            )

    def history(self, *, context_key: str | None = None) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            if context_key is None:
                rows = connection.execute(
                    "SELECT payload FROM tasks ORDER BY updated_at DESC LIMIT 100"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM tasks WHERE context_key=? ORDER BY updated_at DESC",
                    (context_key,),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def related(self, evaluation_id: str, contexts: set[str]) -> list[dict[str, Any]]:
        clause = "json_extract(payload, '$.source.evaluation_id')=?"
        parameters = [evaluation_id]
        if contexts:
            clause += " OR context_key IN (" + ",".join("?" for _ in contexts) + ")"
            parameters.extend(sorted(contexts))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT json_remove(payload, '$.baseline_manifest', '$.source.trials', '$.evaluations') "
                "FROM tasks WHERE " + clause + " ORDER BY updated_at DESC",
                parameters,
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def source_history(self, kind: str, source_id: str, bot_id: str) -> list[dict[str, Any]]:
        args: tuple[str, ...]
        if kind == "robot_task":
            clause = "json_extract(payload, '$.source.run_id')=? AND json_extract(payload, '$.source.bot_id')=?"
            args = (source_id, bot_id)
        else:
            clause = "json_extract(payload, '$.source.evaluation_id')=?"
            args = (source_id,)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM tasks WHERE " + clause + " ORDER BY updated_at DESC", args
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def page(self, *, page: int, limit: int, search: str, status: str) -> dict[str, Any]:
        if page < 1 or not 1 <= limit <= 100 or len(search) > 256:
            raise ValueError("无效的历史查询参数")
        clause = (
            "(?='' OR status=?) AND (?='' OR instr(lower(task_id || ' ' || "
            "COALESCE(json_extract(payload, '$.source.evaluation_id'),'') || ' ' || "
            "COALESCE(json_extract(payload, '$.source.case_instance_id'),'') || ' ' || "
            "COALESCE(json_extract(payload, '$.source.run_id'),'')), lower(?))>0)"
        )
        args = (status, status, search, search)
        with self.database.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE " + clause, args
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT json_remove(payload, '$.baseline_manifest', "
                "'$.source.evidence', '$.source.trials', '$.evaluations') FROM tasks WHERE "
                + clause
                + " ORDER BY updated_at DESC,task_id DESC LIMIT ? OFFSET ?",
                (*args, limit, (page - 1) * limit),
            ).fetchall()
        return {
            "tasks": [json.loads(row[0]) for row in rows],
            "total": total,
            "page": page,
            "limit": limit,
        }
