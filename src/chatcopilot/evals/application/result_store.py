"""Service-owned result database; only explicitly registered new runs are indexed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.core.private_sqlite import PrivateDatabase, json_text
from chatcopilot.evals.result_codec import validate_result, trial_from_dict
from chatcopilot.evals.case_instances import case_instance_identity, validate_case_instance_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_cases (
 snapshot_id TEXT PRIMARY KEY, payload TEXT NOT NULL
);

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
CREATE TABLE IF NOT EXISTS case_instances (
 case_instance_id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL,
 trial_id TEXT NOT NULL, case_ref TEXT NOT NULL, target_id TEXT NOT NULL, attempt INTEGER NOT NULL,
 UNIQUE(evaluation_id, trial_id), UNIQUE(evaluation_id, case_ref, target_id, attempt)
);
"""


class EvaluationResultStore:
    def __init__(self, root: Path) -> None:
        self.database = PrivateDatabase(root / "results.sqlite3", _SCHEMA)

    def register_case(self, value: dict[str, Any]) -> dict[str, Any]:
        from chatcopilot.evals.agent_case import case_identity, validate_case
        case = validate_case(value)
        ident = case_identity(case)
        raw = json_text(case)
        with self.database.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO agent_cases VALUES(?,?)", (ident, raw))
            stored = connection.execute("SELECT payload FROM agent_cases WHERE snapshot_id=?", (ident,)).fetchone()
            if stored[0] != raw:
                raise ValueError("frozen Case content identity conflict")
        return {"snapshot_id": ident, "case": case}

    def frozen_case(self, snapshot_id: str) -> dict[str, Any]:
        from chatcopilot.evals.agent_case import evaluation_cases
        with self.database.connect() as connection:
            row = connection.execute("SELECT payload FROM agent_cases WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        result = {"snapshot_id": snapshot_id, "case": json.loads(row[0])}
        evaluation_cases(result)
        return result

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
        if result:
            validate_result(result)
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

    def identify_trials(self, evaluation_id: str, trials: list[Any]) -> list[dict[str, Any]]:
        """Index verified identities without changing stored results or importing old files."""
        identified = []
        with self.database.connect(write=True) as connection:
            for trial in trials:
                if not isinstance(trial, Mapping):
                    continue
                value = {key: item for key, item in trial.items() if key != "case_instance_id"}
                trial_from_dict(value)
                identity = case_instance_identity(evaluation_id, trial)
                if identity is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO case_instances VALUES(:case_instance_id,:evaluation_id,:trial_id,:case_ref,:target_id,:attempt)",
                        identity,
                    )
                    row = connection.execute(
                        "SELECT * FROM case_instances WHERE case_instance_id=?", (identity["case_instance_id"],)
                    ).fetchone()
                    if row is None or dict(row) != identity:
                        raise ValueError("Case instance identity changed")
                    value["case_instance_id"] = identity["case_instance_id"]
                identified.append(value)
        return identified

    def case_instance(self, case_instance_id: str) -> dict[str, Any]:
        validate_case_instance_id(case_instance_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM case_instances WHERE case_instance_id=?", (case_instance_id,)
            ).fetchone()
        if row is None:
            raise KeyError(case_instance_id)
        return dict(row)

    def delete(self, evaluation_id: str) -> None:
        with self.database.connect(write=True) as connection:
            connection.execute("DELETE FROM case_instances WHERE evaluation_id=?", (evaluation_id,))
            connection.execute("DELETE FROM evaluations WHERE evaluation_id=?", (evaluation_id,))
