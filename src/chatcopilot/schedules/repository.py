"""Private transactional definitions and run snapshots, shared with the bot host."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Iterator

from chatcopilot.core.private_sqlite import PrivateDatabase, json_text


_SCHEMA = """
CREATE TABLE tasks(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL,
 next_run REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE runs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL, created_at REAL NOT NULL, payload TEXT NOT NULL);
CREATE INDEX runs_task ON runs(task_id, created_at DESC, id DESC);
CREATE INDEX runs_status ON runs(status, created_at);
CREATE TABLE host(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
"""


class ScheduleRepository:
    def __init__(self, root: Path):
        self.db = PrivateDatabase(root / "schedules" / "schedules.sqlite3", _SCHEMA)

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator["ScheduleTransaction"]:
        with self.db.connect(write=write) as connection:
            yield ScheduleTransaction(connection)


class ScheduleTransaction:
    def __init__(self, connection):
        self.connection = connection

    def tasks(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM tasks ORDER BY created_at,id")]

    def task(self, task_id: str) -> dict | None:
        row = self.connection.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_task(self, task: dict) -> None:
        self.connection.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "revision=excluded.revision,payload=excluded.payload,next_run=excluded.next_run,updated_at=excluded.updated_at",
            (task["id"], task["revision"], json_text(task), task["next_run"], task["created_at"], task["updated_at"]))

    def delete_task(self, task_id: str) -> None:
        self.connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def run(self, run_id: str) -> dict | None:
        row = self.connection.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def requested_run(self, request_key: str) -> dict | None:
        row = self.connection.execute("SELECT payload FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def runs(self, task_id: str | None = None, *, active: bool = False, limit: int = 50, offset: int = 0) -> list[dict]:
        conditions, params = [], []
        if task_id is not None:
            conditions.append("task_id=?")
            params.append(task_id)
        if active:
            conditions.append("status IN ('queued','researching','delivering')")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = "SELECT payload FROM runs" + where + " ORDER BY created_at DESC,id DESC"
        rows = (self.connection.execute(sql, params) if active else
                self.connection.execute(sql + " LIMIT ? OFFSET ?", (*params, limit, offset)))
        return [json.loads(row[0]) for row in rows]

    def save_run(self, run: dict) -> None:
        self.connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "status=excluded.status,payload=excluded.payload",
            (run["id"], run["task_id"], run["request_key"], run["status"], run["created_at"], json_text(run)))

    def host(self) -> dict | None:
        row = self.connection.execute("SELECT payload FROM host WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save_host(self, value: dict) -> None:
        self.connection.execute("INSERT INTO host VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (json_text(value),))
