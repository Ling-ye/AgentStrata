"""Durable runtime switch intent and reconciliation; never switch or start installed code."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import uuid

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.private_sqlite import PrivateDatabase, private_file, sqlite_uri
from chatcopilot.core.storage_files import long_path
from chatcopilot.deployment.runtime_acceptance import (
    InstallationBinding, RuntimeAcceptance, admitted, canonical, digest, keys, pin,
)

_DB_ID = 0x4153524A
_SCHEMA = f"""
PRAGMA application_id={_DB_ID};
CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL,
 generation INTEGER NOT NULL, selected TEXT, pending TEXT);
CREATE TABLE operations (id TEXT PRIMARY KEY, intent TEXT NOT NULL, record TEXT NOT NULL);
"""


def _identifier(value):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise InstallError("Runtime operation identifier must be a fixed lowercase UUID token")
    return value


class RuntimeUpdateJournal:
    """Separate native dependency-image metadata, never M5 current.json or business data.

    Independent acceptance and selection-observation pins are caller inputs on
    every operation. Persisted JSON/SQLite rows are not an admission authority.
    No filesystem selection, service operation or installation is implemented.
    """
    def __init__(self, path: Path, binding: InstallationBinding):
        if type(binding) is not InstallationBinding:
            raise InstallError("Runtime journal requires a trusted installation binding")
        if not path.is_absolute() or path.name != "runtime-update.sqlite":
            raise InstallError("Runtime journal requires an explicit private runtime-update.sqlite path")
        path = long_path(path)
        if path.exists() or path.is_symlink():
            private_file(path)
            try:
                with closing(sqlite3.connect(sqlite_uri(path, mode="ro"), uri=True)) as existing:
                    if (existing.execute("PRAGMA application_id").fetchone()[0] != _DB_ID
                            or existing.execute("PRAGMA user_version").fetchone()[0] != 1):
                        raise InstallError("Existing file is not a runtime update journal; preserved without repair")
            except sqlite3.Error:
                raise InstallError("Existing runtime journal is invalid; preserved without repair") from None
        self.identity = binding.identity_sha256
        self.db = PrivateDatabase(path, _SCHEMA)
        with self.db.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO state VALUES (1,?,0,NULL,NULL)", (self.identity,))
            self._state(connection)

    def _state(self, connection):
        row = connection.execute("SELECT identity,generation,selected,pending FROM state WHERE id=1").fetchone()
        if row is None or row[0] != self.identity:
            raise InstallError("Runtime journal belongs to another installation identity")
        if type(row[1]) is not int or not 0 <= row[1] < 9223372036854775807:
            raise InstallError("Runtime journal generation is invalid")
        if row[2] is not None:
            pin(row[2])
        if row[3] is not None:
            _identifier(row[3])
        return {"identity_sha256": row[0], "generation": row[1], "selected_sha256": row[2], "pending": row[3]}

    def inspect(self):
        with self.db.connect() as connection:
            return self._state(connection)

    def operation(self, operation_id):
        with self.db.connect() as connection:
            self._state(connection)
            return self._operation(connection, operation_id)

    @staticmethod
    def _operation(connection, operation_id):
        operation_id = _identifier(operation_id)
        row = connection.execute("SELECT record,intent FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise InstallError("Unknown runtime update operation")
        record = json.loads(row[0])
        intent_keys = {"id", "kind", "identity_sha256", "generation", "before_sha256", "target_sha256",
                       "target_requirements_sha256", "rollback_of"}
        if "selection_before_sha256" in record:
            intent_keys.add("selection_before_sha256")
            pin(record["selection_before_sha256"])
        keys(record, intent_keys | {"intent_sha256", "state", "observations"})
        if (record["id"] != operation_id or record["intent_sha256"] != row[1]
                or digest(canonical({name:record[name] for name in intent_keys})) != row[1]
                or record["state"] not in ("prepared", "recorded", "restored", "recovery_required")
                or type(record["observations"]) is not list or len(record["observations"]) > 128):
            raise InstallError("Runtime journal intent or observation history changed")
        for observation_pin in record["observations"]:
            pin(observation_pin)
        return record

    def prepare(self, target: RuntimeAcceptance, *, current: RuntimeAcceptance | None,
            expected_generation: int, operation_id: str | None = None, rollback_of: str | None = None,
            selection_before_sha256: str | None = None):
        if (type(target) is not RuntimeAcceptance or target.binding.identity_sha256 != self.identity
                or (current is not None and (type(current) is not RuntimeAcceptance or current.binding.identity_sha256 != self.identity))
                or type(expected_generation) is not int or expected_generation < 0):
            raise InstallError("Runtime update requires admitted versions for this installation and an exact generation")
        operation_id = _identifier(uuid.uuid4().hex if operation_id is None else operation_id)
        if rollback_of is not None:
            _identifier(rollback_of)
        before = current.receipt_sha256 if current else None
        after = pin(target.receipt_sha256)
        if before == after:
            raise InstallError("Runtime update must select a distinct admitted image")
        intent = {"id": operation_id, "kind": "rollback" if rollback_of else "update", "identity_sha256": self.identity,
            "generation": expected_generation, "before_sha256": before, "target_sha256": after,
            "target_requirements_sha256": pin(target.requirements_sha256), "rollback_of": rollback_of}
        if selection_before_sha256 is not None:
            intent["selection_before_sha256"] = pin(selection_before_sha256)
        intent_sha = digest(canonical(intent))
        with self.db.connect(write=True) as connection:
            state = self._state(connection)
            old = connection.execute("SELECT intent FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old is not None:
                if old[0] != intent_sha:
                    raise InstallError("Runtime operation retry changed its frozen intent")
                return self._operation(connection, operation_id)
            if state["generation"] != expected_generation or state["selected_sha256"] != before or state["pending"] is not None:
                raise InstallError("Runtime selection is stale or has an unresolved operation")
            if rollback_of is not None:
                origin = self._operation(connection, rollback_of)
                if (origin["kind"] != "update" or origin["state"] != "recorded" or origin["target_sha256"] != before
                        or origin["before_sha256"] != after or origin["generation"] + 1 != expected_generation):
                    raise InstallError("Only the immediately selected update can be rolled back to its independently admitted predecessor")
            record = {**intent, "intent_sha256": intent_sha, "state": "prepared", "observations": []}
            connection.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, intent_sha, canonical(record).decode()))
            connection.execute("UPDATE state SET pending=? WHERE id=1", (operation_id,))
            return record

    def reconcile(self, operation_id, observation: bytes, *, expected_observation_sha256: str):
        observed = admitted(observation, expected_observation_sha256)
        keys(observed, {"schema", "identity_sha256", "intent_sha256", "selected_sha256", "verification_sha256"})
        if observed["schema"] != "agentstrata-runtime-switch-observation/v1" or observed["identity_sha256"] != self.identity:
            raise InstallError("Runtime switch observation belongs to another contract or installation")
        pin(observed["verification_sha256"])
        selected = observed["selected_sha256"]
        if selected is not None:
            pin(selected)
        with self.db.connect(write=True) as connection:
            state = self._state(connection)
            record = self._operation(connection, operation_id)
            if observed["intent_sha256"] != record["intent_sha256"]:
                raise InstallError("Runtime switch observation belongs to another operation")
            if expected_observation_sha256 in record["observations"]:
                return record
            if len(record["observations"]) >= 128:
                raise InstallError("Runtime reconciliation observation budget exceeded; manual review required")
            if (state["pending"] != operation_id or state["generation"] != record["generation"]
                    or state["selected_sha256"] != record["before_sha256"]):
                raise InstallError("Runtime switch cannot overwrite a later or resolved selection")
            record["observations"].append(expected_observation_sha256)
            if selected == record["target_sha256"]:
                record["state"] = "recorded"
                connection.execute("UPDATE state SET selected=?,generation=generation+1,pending=NULL WHERE id=1", (selected,))
            elif selected == record["before_sha256"]:
                record["state"] = "restored"
                connection.execute("UPDATE state SET pending=NULL WHERE id=1")
            else:
                record["state"] = "recovery_required"
            connection.execute("UPDATE operations SET record=? WHERE id=?", (canonical(record).decode(), operation_id))
            return record
