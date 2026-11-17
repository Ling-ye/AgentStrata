"""Atomic selector and immutable selector history for a fresh data-only runtime lab."""
from contextlib import closing
import json
import re
from pathlib import Path
import sqlite3

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.private_sqlite import PrivateDatabase, private_file, sqlite_uri
from chatcopilot.core.storage_files import long_path
from chatcopilot.deployment.runtime_acceptance import canonical, digest, keys, pin

_ID = 0x41534C53
_SCHEMA = f"""PRAGMA application_id={_ID};
CREATE TABLE selection (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE history (sha256 TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE recovery_fence (id INTEGER PRIMARY KEY CHECK(id=1), operation TEXT);
"""


class IsolatedSelection:
    def __init__(self, path: Path, anchor_sha256: str):
        if not path.is_absolute() or path.name != "runtime-selection.sqlite":
            raise InstallError("Isolated selection requires its explicit private database path")
        self.anchor = pin(anchor_sha256)
        path = long_path(path)
        if path.exists() or path.is_symlink():
            private_file(path)
            try:
                with closing(sqlite3.connect(sqlite_uri(path, mode="ro"), uri=True)) as old:
                    if old.execute("PRAGMA application_id").fetchone()[0] != _ID or old.execute("PRAGMA user_version").fetchone()[0] != 1:
                        raise InstallError("Existing database is not an isolated selector; preserved without repair")
            except sqlite3.Error:
                raise InstallError("Invalid isolated selector database; preserved without repair") from None
        self.db = PrivateDatabase(path, _SCHEMA)
        initial = {"schema":"agentstrata-isolated-runtime-selection/v1", "anchor_sha256":self.anchor,
            "generation":0, "acceptance_sha256":None, "intent_sha256":None}
        raw = canonical(initial).decode()
        with self.db.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO selection VALUES (1,?)", (raw,))
            connection.execute("INSERT OR IGNORE INTO history VALUES (?,?)", (digest(raw.encode()), raw))
            connection.execute("INSERT OR IGNORE INTO recovery_fence VALUES (1,NULL)")
            self._read(connection)

    def blocked(self):
        with self.db.connect() as connection:
            row = connection.execute("SELECT operation FROM recovery_fence WHERE id=1").fetchone()
            if row is None or (row[0] is not None and (type(row[0]) is not str or not re.fullmatch(r"[a-f0-9]{32}", row[0]))):
                raise InstallError("Isolated recovery fence is invalid")
            return row[0]

    def fence(self, operation_id):
        if type(operation_id) is not str or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise InstallError("Isolated recovery fence requires a fixed operation identifier")
        with self.db.connect(write=True) as connection:
            old = connection.execute("SELECT operation FROM recovery_fence WHERE id=1").fetchone()[0]
            if old not in (None, operation_id):
                raise InstallError("Another isolated recovery fence is unresolved")
            connection.execute("UPDATE recovery_fence SET operation=? WHERE id=1", (operation_id,))

    def clear_fence(self, operation_id):
        with self.db.connect(write=True) as connection:
            old = connection.execute("SELECT operation FROM recovery_fence WHERE id=1").fetchone()[0]
            if old not in (None, operation_id):
                raise InstallError("Cannot clear another operation's recovery fence")
            connection.execute("UPDATE recovery_fence SET operation=NULL WHERE id=1")

    def transfer_fence(self, before_operation, after_operation):
        if any(type(value) is not str or not re.fullmatch(r"[a-f0-9]{32}", value) for value in (before_operation, after_operation)):
            raise InstallError("Isolated recovery fence transfer requires fixed operation identifiers")
        with self.db.connect(write=True) as connection:
            if connection.execute("SELECT operation FROM recovery_fence WHERE id=1").fetchone()[0] != before_operation:
                raise InstallError("Isolated recovery fence changed before transfer")
            connection.execute("UPDATE recovery_fence SET operation=? WHERE id=1", (after_operation,))

    def validate(self, value):
        keys(value, {"schema", "anchor_sha256", "generation", "acceptance_sha256", "intent_sha256"})
        if (value["schema"] != "agentstrata-isolated-runtime-selection/v1" or value["anchor_sha256"] != self.anchor
                or type(value["generation"]) is not int or not 0 <= value["generation"] < 9223372036854775807):
            raise InstallError("Isolated selector belongs to another anchor or generation")
        if value["acceptance_sha256"] is None:
            if value["generation"] != 0 or value["intent_sha256"] is not None:
                raise InstallError("Empty isolated selection has an invalid generation or intent")
        else:
            pin(value["acceptance_sha256"])
            pin(value["intent_sha256"])
        return value

    def _read(self, connection):
        row = connection.execute("SELECT value FROM selection WHERE id=1").fetchone()
        if row is None or type(row[0]) is not str or len(row[0]) > 2048:
            raise InstallError("Isolated selector record is missing or exceeds its budget")
        return self.validate(json.loads(row[0]))

    def read(self):
        with self.db.connect() as connection:
            return self._read(connection)

    def historical(self, expected_sha256):
        expected_sha256 = pin(expected_sha256)
        with self.db.connect() as connection:
            row = connection.execute("SELECT value FROM history WHERE sha256=?", (expected_sha256,)).fetchone()
            if row is None or type(row[0]) is not str or len(row[0]) > 2048 or digest(row[0].encode()) != expected_sha256:
                raise InstallError("Frozen before-selection record is missing or changed")
            return self.validate(json.loads(row[0]))

    def compare_and_select(self, before_sha256, desired):
        before_sha256 = pin(before_sha256)
        self.validate(desired)
        raw = canonical(desired)
        key = digest(raw)
        with self.db.connect(write=True) as connection:
            if digest(canonical(self._read(connection))) != before_sha256:
                raise InstallError("Isolated selection changed before the atomic switch")
            old = connection.execute("SELECT value FROM history WHERE sha256=?", (key,)).fetchone()
            if old is not None and old[0] != raw.decode():
                raise InstallError("Isolated selection history changed")
            connection.execute("INSERT OR IGNORE INTO history VALUES (?,?)", (key, raw.decode()))
            connection.execute("UPDATE selection SET value=? WHERE id=1", (raw.decode(),))
        return desired
