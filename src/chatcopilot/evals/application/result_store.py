"""Service-owned result database; only explicitly registered new runs are indexed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.core.private_sqlite import PrivateDatabase, json_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
 evaluation_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT,
 observation TEXT, result_hash TEXT, state TEXT NOT NULL DEFAULT '{}',
 ingestion_state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS trials (
 evaluation_id TEXT NOT NULL REFERENCES evaluations(evaluation_id) ON DELETE CASCADE,
 trial_id TEXT NOT NULL, case_ref TEXT NOT NULL, target_id TEXT NOT NULL,
 attempt INTEGER NOT NULL, outcome TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(evaluation_id, trial_id), UNIQUE(evaluation_id, case_ref, target_id, attempt)
);
CREATE INDEX IF NOT EXISTS trials_case ON trials(case_ref, target_id, outcome);
"""


class EvaluationResultStore:
    def __init__(self, root: Path) -> None:
        self.database = PrivateDatabase(root / "results.sqlite3", _SCHEMA)

    def register(self, request: Mapping[str, Any]) -> None:
        key = str(request["evaluation_id"])
        payload = json_text(dict(request))
        with self.database.connect(write=True) as connection:
            existing = connection.execute(
                "SELECT request FROM evaluations WHERE evaluation_id=?", (key,)
            ).fetchone()
            if existing and existing[0] != payload:
                raise ValueError("registered Evaluation request changed")
            connection.execute(
                "INSERT OR IGNORE INTO evaluations(evaluation_id,request) VALUES(?,?)",
                (key, payload),
            )

    def contains(self, evaluation_id: str) -> bool:
        with self.database.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM evaluations WHERE evaluation_id=?", (evaluation_id,)
                ).fetchone()
                is not None
            )

    def synchronize(
        self,
        evaluation_id: str,
        *,
        result: Mapping[str, Any],
        state: Mapping[str, Any],
        observation: Mapping[str, Any] | None = None,
    ) -> None:
        raw = json_text(dict(result))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        rows = result.get("trials", [])
        if not isinstance(rows, list):
            raise ValueError("Evaluation trials must be a list")
        with self.database.connect(write=True) as connection:
            registered = connection.execute(
                "SELECT result_hash,ingestion_state FROM evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if registered is None:
                return
            if result and result.get("evaluation_id") != evaluation_id:
                raise ValueError("Evaluation result identity mismatch")
            if registered["ingestion_state"] == "ready" and registered["result_hash"] != digest:
                raise ValueError("completed Evaluation result changed after ingestion")
            if registered["ingestion_state"] == "ready":
                return
            connection.execute("DELETE FROM trials WHERE evaluation_id=?", (evaluation_id,))
            for trial in rows:
                if not isinstance(trial, dict) or trial.get("evaluation_id") != evaluation_id:
                    raise ValueError("Trial identity does not match Evaluation")
                connection.execute(
                    "INSERT INTO trials VALUES(?,?,?,?,?,?,?)",
                    (
                        evaluation_id,
                        trial["trial_id"],
                        trial.get("case_ref") or trial["case_id"],
                        trial["target_id"],
                        trial["attempt"],
                        trial["outcome"],
                        json_text(trial),
                    ),
                )
            complete = bool(result) and state.get("status") in {
                "completed",
                "partial",
                "cancelled",
                "interrupted",
                "error",
            }
            connection.execute(
                "UPDATE evaluations SET result=?,result_hash=?,state=?,observation=?,ingestion_state=? WHERE evaluation_id=?",
                (
                    raw,
                    digest,
                    json_text(dict(state)),
                    json_text(dict(observation or {})),
                    "ready" if complete else "pending",
                    evaluation_id,
                ),
            )

    def get(self, evaluation_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "request": json.loads(row["request"]),
            "result": json.loads(row["result"] or "{}"),
            "observation": json.loads(row["observation"] or "{}"),
            "ingestion_state": row["ingestion_state"],
        }

    def pending(self) -> list[str]:
        with self.database.connect() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT evaluation_id FROM evaluations WHERE ingestion_state='pending'"
                )
            ]

    def delete(self, evaluation_id: str) -> None:
        with self.database.connect(write=True) as connection:
            connection.execute("DELETE FROM evaluations WHERE evaluation_id=?", (evaluation_id,))
