"""Harness owns only tasks and attempts; evaluation results remain external."""

from __future__ import annotations

import json
import hashlib
import logging
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import PrivateDatabase, json_text, private_lock, storage_error_details
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
CREATE TABLE IF NOT EXISTS repair_steps (
 task_id TEXT NOT NULL REFERENCES tasks(task_id), step_id TEXT NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(task_id, step_id)
);
"""


class HarnessStore:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.database = PrivateDatabase(self.root / "harness.sqlite3", _SCHEMA)

    @contextmanager
    def control_guard(self, task_id: str, *, wait: bool = False):
        """Serialize control operations without holding a database transaction."""
        digest = hashlib.sha256(task_id.encode()).hexdigest()
        with self.creation_guard(), ExitStack() as stack:
            try:
                stack.enter_context(private_lock(self.root / ("control-" + digest + ".lock"),
                                                timeout=None if wait else 5))
            except (BlockingIOError, TimeoutError) as exc:
                raise HarnessError("conflict", "该任务的控制操作尚未结束，请稍后重试") from exc
            yield

    def active_tasks(self, pipeline_version: int) -> list[str]:
        with self.database.connect() as connection:
            placeholders = ",".join("?" for _ in ACTIVE)
            return [row[0] for row in connection.execute(
                f"SELECT task_id FROM tasks WHERE status IN ({placeholders}) "
                "AND json_extract(payload, '$.pipeline_version')=?", (*ACTIVE, pipeline_version))]

    @contextmanager
    def creation_guard(self, *, exclusive: bool = False):
        """Admission and maintenance share a stable lock separate from SQLite."""
        with ExitStack() as stack:
            try:
                descriptor = stack.enter_context(private_lock(self.root / "harness.lock", exclusive=exclusive))
            except BlockingIOError as exc:
                raise HarnessError("maintenance_active", "Harness 正在维护或接受另一项操作，请稍后重试") from exc
            yield descriptor

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

    def register_trace(self, task_id: str, root: Path, reference: dict[str, Any]) -> None:
        relative = root.absolute().relative_to((self.root / "jobs" / task_id).absolute())
        if any(part in {".", ".."} for part in relative.parts):
            raise ValueError("Trace root is outside this Harness task")
        current = self.get(task_id)
        records = dict(current.get("trace_records", {}))
        records[reference["trace_ref"]] = {**reference, "directory": relative.as_posix()}
        self.update(task_id, trace_records=records)

    def save_flow_step(self, task_id: str, step: dict[str, Any]) -> None:
        """Merge one observation without racing cancellation or other task updates."""
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise HarnessError("not_found", "修复任务不存在")
            task = json.loads(row[0])
            if (task.get("pipeline_version") or 0) >= 8:
                old = connection.execute("SELECT payload FROM repair_steps WHERE task_id=? AND step_id=?",
                                         (task_id, step["id"])).fetchone()
                if old and json.loads(old[0])["status"] != "running":
                    return
                if task["status"] in {"cancel_requested", "cancelled", "interrupted", "blocked", "failed", "needs_review"}:
                    step = {**step, "status": "cancelled" if task["status"] in {"cancel_requested", "cancelled"} else "interrupted",
                            "conclusion": "任务已停止；本步骤未确认完成", "finished_at": time.time()}
                connection.execute("INSERT INTO repair_steps VALUES(?,?,?) ON CONFLICT(task_id,step_id) DO UPDATE SET payload=excluded.payload",
                                   (task_id, step["id"], json_text(step)))
                return
            steps = task.setdefault("flow_steps", [])
            index = next((i for i, value in enumerate(steps) if value["id"] == step["id"]), len(steps))
            if index < len(steps) and steps[index]["status"] != "running":
                return
            # Late observations never turn a cancelled task's active step into success.
            if task.get("status") in {"cancel_requested", "cancelled", "interrupted"}:
                step = {**step, "status": "cancelled" if task["status"] != "interrupted" else "interrupted",
                        "conclusion": "任务已取消或中断；本步骤未确认完成", "finished_at": time.time()}
            if index == len(steps):
                steps.append(step)
            else:
                steps[index] = step
            task["flow_version"] = 1
            connection.execute("UPDATE tasks SET payload=? WHERE task_id=?", (json_text(task), task_id))

    def flow_steps(self, task_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT payload FROM repair_steps WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(
        self, task_id: str, *, if_status: frozenset[str] | None = None,
        accepted_attempt: dict[str, Any] | None = None, **changes: Any
    ) -> dict[str, Any]:
        if accepted_attempt is not None and (
            accepted_attempt.get("status") != "accepted"
            or accepted_attempt.get("review", {}).get("decision") != "approved"
            or accepted_attempt.get("candidate_digest") != changes.get("verified_digest")
        ):
            raise ValueError("checkpoint requires its independently approved attempt")
        from chatcopilot.core.trace_capture import current_capture
        capture = current_capture()
        if capture and any(key in changes for key in ("stage", "status", "evaluations", "verification_plan")):
            capture.record({"kind": "harness_phase", "status": str(changes.get("status", "recorded")),
                            "data": {"name": changes.get("stage", "state_update")}}, changes)
        with self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise HarnessError("not_found", "修复任务不存在")
            current = json.loads(row[0])
            if accepted_attempt is not None and current["status"] != "running":
                raise Cancelled()
            if if_status is not None and current["status"] not in if_status:
                return current
            if current["status"] in {"cancel_requested", "cancelled"} and changes.get("status") in {
                "running",
                "fixed",
                "needs_review",
            }:
                raise Cancelled()
            value = {**current, **changes, "updated_at": time.time()}
            if "status" in changes and value["status"] not in ACTIVE:
                from chatcopilot.harness.flow_receipts import close_unfinished_steps
                close_unfinished_steps(value, value["updated_at"])
                if (value.get("pipeline_version") or 0) >= 8:
                    rows = connection.execute("SELECT step_id,payload FROM repair_steps WHERE task_id=?", (task_id,)).fetchall()
                    for ident, payload in rows:
                        observation = json.loads(payload)
                        if observation["status"] == "running":
                            observation.update(status="cancelled" if value["status"] == "cancelled" else "interrupted",
                                               interrupted_at=value["updated_at"], conclusion="任务已停止；本步骤未记录可靠终态")
                            connection.execute("UPDATE repair_steps SET payload=? WHERE task_id=? AND step_id=?",
                                               (json_text(observation), task_id, ident))
            if any(key in changes for key in ("delivery", "cleanup", "local_commit")):
                from chatcopilot.harness.flow_receipts import record_delivery
                record_delivery(current, value, value["updated_at"])
            active_key = (
                value["active_key"]
                if value["status"] in ACTIVE or value["status"] == "waiting_input"
                or value.get("current_evaluation_id") or value.get("delivery_evaluation")
                else None
            )
            connection.execute(
                "UPDATE tasks SET status=?,active_key=?,payload=?,updated_at=? WHERE task_id=?",
                (value["status"], active_key, json_text(value), value["updated_at"], task_id),
            )
            if accepted_attempt is not None:
                connection.execute(
                    "INSERT INTO attempts VALUES(?,?,?) ON CONFLICT(task_id,number) DO UPDATE SET payload=excluded.payload",
                    (task_id, accepted_attempt["number"], json_text(accepted_attempt)),
                )
        return value

    def interrupt(self, task_id: str, *, storage_error: BaseException | None = None,
                  if_status: frozenset[str] = ACTIVE) -> dict[str, Any]:
        """Persist terminal task/attempt state together; never replay failed work."""
        details = storage_error_details(storage_error) if storage_error is not None else None
        if storage_error is not None and details is None:
            details = {"database": self.database.path.name, "phase": "worker",
                       "type": type(storage_error).__name__,
                       "sqlite_errorcode": getattr(storage_error, "sqlite_errorcode", None),
                       "sqlite_errorname": getattr(storage_error, "sqlite_errorname", None)}
        code = "storage_error" if storage_error is not None else "worker_interrupted"
        message = "宿主存储错误，任务已停止；保留已持久化的验收检查点。请检查 worker 日志。" if storage_error is not None else "worker 已停止，本次执行已中断"
        try:
            with self.database.connect(write=True) as connection:
                row = connection.execute("SELECT payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if row is None:
                    raise HarnessError("not_found", "修复任务不存在")
                task = json.loads(row[0])
                if storage_error is None and task["status"] not in if_status:
                    return task
                now = time.time()
                task.update(status="blocked" if storage_error is not None else "interrupted", stage="done",
                            error_code=code, stop_reason=code, message=message, updated_at=now,
                            current_source=None, current_group=None, current_attempt=None)
                from chatcopilot.harness.flow_receipts import close_unfinished_steps
                close_unfinished_steps(task, now)
                if details is not None:
                    task["storage_error"] = details
                for group in task.get("governance", {}).get("groups", []):
                    if group["status"] in {"preparing", "coding"}:
                        group.update(status="interrupted", reason=message, error_code=code)
                for batch in task.get("governance", {}).get("coverage") or []:
                    if batch["status"] == "running":
                        batch.update(status="interrupted", reason=message, error_code=code)
                for row in connection.execute("SELECT number,payload FROM attempts WHERE task_id=?", (task_id,)).fetchall():
                    attempt = json.loads(row[1])
                    if attempt["status"] in {"accepted", "rejected", "coding_failed", "interrupted", "rerouted"}:
                        continue
                    attempt.update(status="interrupted", error_code=code, error=message,
                                   finished_at=now, counts_toward_budget=storage_error is None)
                    if details is not None:
                        attempt["storage_error"] = details
                    connection.execute("UPDATE attempts SET payload=? WHERE task_id=? AND number=?",
                                       (json_text(attempt), task_id, row[0]))
                connection.execute("UPDATE tasks SET status=?,active_key=?,payload=?,updated_at=? WHERE task_id=?",
                                   (task["status"], task["active_key"] if task.get("current_evaluation_id") or task.get("delivery_evaluation") else None,
                                    json_text(task), now, task_id))
                return task
        except Exception:
            if storage_error is not None:
                logging.getLogger(__name__).error("Storage failure terminal state could not be persisted; worker must exit")
                raise storage_error
            raise

    def save_attempt(self, task_id: str, number: int, payload: dict[str, Any]) -> None:
        from chatcopilot.core.trace_capture import current_capture
        capture = current_capture()
        if capture:
            capture.record({"kind": "harness_attempt", "status": payload.get("status", "recorded"),
                            "data": {"name": f"attempt-{number}"}}, payload)
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

    def claim_image(self, task_id: str, reference: dict[str, str]) -> tuple[dict[str, Any], bool]:
        with self.creation_guard(), self.database.connect(write=True) as connection:
            row = connection.execute("SELECT payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise HarnessError("not_found", "修复任务不存在")
            task = json.loads(row[0])
            if task["status"] != "waiting_input":
                if task["source"].get("image_resources") == [reference]:
                    return task, False
                raise HarnessError("conflict", "任务已接受其他输入")
            task.update(status="queued", dispatch_state="creating", next_action=None, error_code=None,
                        message="原图已补充，自动继续", updated_at=time.time(),
                        source={**task["source"], "image_resources": [reference]})
            connection.execute("UPDATE tasks SET status='queued',active_key=?,payload=?,updated_at=? WHERE task_id=?",
                               (task["active_key"], json_text(task), task["updated_at"], task_id))
            return task, True

    def claim_resume(self, task_id: str) -> tuple[dict[str, Any], bool]:
        with self.creation_guard(), self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise HarnessError("not_found", "修复任务不存在")
            task = json.loads(row[0])
            if task["status"] not in {"blocked", "interrupted", "cancelled", "waiting_input"}:
                return task, False
            task.update(
                status="queued",
                dispatch_state="creating",
                message="准备继续",
                error_code=None, next_action=None,
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
            if task.get("delivery") and attempt.get("review", {}).get("decision") != "approved":
                raise ValueError("independent review is required")
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
                else "修复验收通过，等待 PR 交付" if task.get("delivery") else "worktree 验收通过；改动尚未提交或合入",
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

    def page(self, *, page: int, limit: int, search: str, status: str, kind: str = "") -> dict[str, Any]:
        if page < 1 or not 1 <= limit <= 100 or len(search) > 256:
            raise ValueError("无效的历史查询参数")
        if kind not in {"", "repair", "code_health"}:
            raise ValueError("未知任务类型")
        clause = (
            "(?='' OR status=?) AND (?='' OR instr(lower(task_id || ' ' || "
            "COALESCE(json_extract(payload, '$.source.evaluation_id'),'') || ' ' || "
            "COALESCE(json_extract(payload, '$.source.case_instance_id'),'') || ' ' || "
            "COALESCE(json_extract(payload, '$.source.run_id'),'')), lower(?))>0)"
        )
        if kind:
            clause += " AND COALESCE(json_extract(payload, '$.source.kind'),'evaluation') " + (
                "= 'code_health'" if kind == "code_health" else "!= 'code_health'")
        args = (status, status, search, search)
        with self.database.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE " + clause, args
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT json_remove(payload, '$.baseline_manifest', "
                "'$.source.evidence', '$.source.trials', '$.evaluations', '$.governance', '$.verified_manifest', '$.progress_sources') FROM tasks WHERE "
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
