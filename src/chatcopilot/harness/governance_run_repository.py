"""Durable batch identities in the existing Harness database."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import time
import uuid

from chatcopilot.core.private_sqlite import json_text, private_lock
from chatcopilot.harness.governance_types import RUN_ACTIVE, RUN_ACTIVE_STATUSES
from chatcopilot.harness.models import HarnessError


# Keep literal values for SQLite's partial index and reuse its exact predicate in queries.
_ACTIVE_STATUS_SQL = "status IN (" + ",".join(f"'{status}'" for status in RUN_ACTIVE_STATUSES) + ")"


class GovernanceRunRepository:
    def __init__(self, store):
        self.store = store
        # Additive Harness-owned schema; never alter the shared database version,
        # re-interpret old tasks, or clear their indexes.
        with store.database.connect() as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='governance_runs'").fetchone()
        if not exists:
            with store.creation_guard(), private_lock(store.root / "governance-schema.lock", timeout=5):
                with store.database.connect(write=True) as connection:
                    connection.execute("""CREATE TABLE IF NOT EXISTS governance_runs (
                        run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                        repository TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
                    connection.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS governance_run_active
                        ON governance_runs(repository) WHERE {_ACTIVE_STATUS_SQL}""")

    @contextmanager
    def locked(self):
        with self.store.creation_guard(), private_lock(self.store.root / "governance-runs.lock", timeout=5):
            yield

    def create(self, repository, options, request_id):
        digest = hashlib.sha256(json_text({"repository": repository, "options": options}).encode()).hexdigest()
        with self.store.database.connect(write=True) as connection:
            old = connection.execute("SELECT payload FROM governance_runs WHERE request_id=?", (request_id,)).fetchone()
            if old:
                value = json.loads(old[0])
                if value["request_digest"] != digest:
                    raise HarnessError("conflict", "同一请求 ID 的内容已变化")
                return value, False
            if connection.execute("SELECT 1 FROM governance_runs WHERE repository=? AND " + _ACTIVE_STATUS_SQL, (repository,)).fetchone():
                raise HarnessError("governance_active", "此仓库已有活动熵回收批次")
            now = time.time()
            value = {"run_id": "gc-" + uuid.uuid4().hex, "request_id": request_id, "request_digest": digest,
                     "repository": repository, "options": options, "status": "running", "stop_reason": "",
                     "current_task_id": None, "sequence": 0, "found_count": 0, "merged_count": 0,
                     "elapsed_seconds": 0.0, "created_at": now, "updated_at": now}
            connection.execute("INSERT INTO governance_runs VALUES(?,?,?,?,?,?,?)",
                (value["run_id"], request_id, repository, value["status"], json_text(value), now, now))
        return value, True

    def by_request(self, request_id):
        with self.store.database.connect() as connection:
            row = connection.execute("SELECT payload FROM governance_runs WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, run_id):
        with self.store.database.connect() as connection:
            row = connection.execute("SELECT payload FROM governance_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise HarnessError("not_found", "熵回收批次不存在")
        return json.loads(row[0])

    def update(self, run_id, **fields):
        with self.store.database.connect(write=True) as connection:
            row = connection.execute("SELECT payload FROM governance_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise HarnessError("not_found", "熵回收批次不存在")
            value = {**json.loads(row[0]), **fields, "updated_at": time.time()}
            connection.execute("UPDATE governance_runs SET status=?,payload=?,updated_at=? WHERE run_id=?",
                (value["status"], json_text(value), value["updated_at"], run_id))
        return value

    def active(self, repository=None):
        with self.store.database.connect() as connection:
            rows = connection.execute("SELECT payload FROM governance_runs WHERE " + _ACTIVE_STATUS_SQL
                + (" AND repository=?" if repository else ""), (repository,) if repository else ()).fetchall()
        return [json.loads(row[0]) for row in rows]

    def tasks(self, run_id):
        with self.store.database.connect() as connection:
            rows = connection.execute("SELECT payload FROM tasks WHERE json_extract(payload,'$.governance_run_id')=? "
                "ORDER BY json_extract(payload,'$.governance_sequence')", (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def project(self, run):
        tasks = self.tasks(run["run_id"])
        values = {"found_count": sum(bool(task.get("governance_finding_id")) and not task.get("skill_learning_origin")
                                      for task in tasks),
                  "merged_count": sum(task.get("delivery", {}).get("state") == "merged" and not task.get("skill_learning_origin")
                                      for task in tasks),
                  "elapsed_seconds": sum(float(task.get("elapsed_seconds", 0)) for task in tasks),
                  "current_task_id": tasks[-1]["task_id"] if tasks else None, "sequence": len(tasks)}
        return {**run, **values, "tasks": [{
            **{key: task[key] for key in (
                "task_id", "status", "stage", "base_commit", "governance_sequence", "governance_summary", "governance_finding_id",
                "message", "stop_reason", "elapsed_seconds", "delivery") if key in task},
            "purpose": "skill_learning" if task.get("skill_learning_origin") else "code_health",
        } for task in tasks]}

    def refresh(self, run_id):
        value = self.project(self.get(run_id))
        return self.update(run_id, **{key: value[key] for key in (
            "current_task_id", "sequence", "found_count", "merged_count", "elapsed_seconds")})

    def page(self, *, repository, page=1, limit=20, search="", status=""):
        if page < 1 or not 1 <= limit <= 100 or status and status not in RUN_ACTIVE | {"completed", "blocked", "cancelled"}:
            raise ValueError("无效的批次分页或状态")
        clause, params = "repository=? AND run_id LIKE ?", [repository, "%" + search + "%"]
        if status:
            clause += " AND status=?"
            params.append(status)
        with self.store.database.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM governance_runs WHERE " + clause, params).fetchone()[0]
            rows = connection.execute("SELECT payload FROM governance_runs WHERE " + clause + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                                      (*params, limit, (page - 1) * limit)).fetchall()
        return {"runs": [self.project(json.loads(row[0])) for row in rows], "total": total}
