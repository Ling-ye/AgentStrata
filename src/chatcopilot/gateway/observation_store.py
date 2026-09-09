"""Private observation index and expiring bodies, separate from Gateway authority."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterator
import uuid

from chatcopilot.core.inspection import fingerprint
from chatcopilot.core.observability_redaction import (
    load_bounded_observability_json, bound_observability_payload,
)
from .state_store import (
    GatewayStateError, _ensure_private_database_file, _ensure_private_root,
    _validate_private_file_metadata, _validate_private_root, _validate_sqlite_files,
)

BODY_LIMIT = 64 * 1024
CONTEXT_LIMIT = 8 * 1024 * 1024
RUN_BODY_LIMIT = 64 * 1024 * 1024
RETENTION_SECONDS = 30 * 86400
TERMINAL = ("completed", "failed", "aborted")
_BODY_ID = re.compile(r"^[a-f0-9]{32}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS configurations(
 config_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, state TEXT NOT NULL, error_code TEXT,
 created_at REAL NOT NULL, started_at REAL, finished_at REAL, updated_at REAL NOT NULL,
 generation INTEGER, channel TEXT, conversation_kind TEXT,
 config_id TEXT, config_revision TEXT, backend TEXT, model TEXT, role TEXT,
 capture_state TEXT NOT NULL DEFAULT 'not_recorded', body_bytes INTEGER NOT NULL DEFAULT 0,
 details_expired INTEGER NOT NULL DEFAULT 0, result_ref TEXT, input_ref TEXT,
 receipts TEXT NOT NULL DEFAULT '[]', outbox TEXT NOT NULL DEFAULT '[]', approvals TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS observation_runs_time ON runs(created_at DESC,run_id DESC);
CREATE INDEX IF NOT EXISTS observation_runs_config ON runs(config_id,created_at);
CREATE INDEX IF NOT EXISTS observation_runs_state ON runs(state,created_at);
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
 kind TEXT NOT NULL, layer TEXT NOT NULL, entity_id TEXT, refs TEXT NOT NULL DEFAULT '[]',
 status TEXT, created_at REAL NOT NULL, trace_id TEXT, span_id TEXT, parent_span_id TEXT,
 phase TEXT, name TEXT, model TEXT, backend TEXT, error_code TEXT,
 elapsed_ms REAL, input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, total_tokens INTEGER,
 metadata TEXT NOT NULL, body_ref TEXT, body_state TEXT NOT NULL DEFAULT 'not_recorded',
 event_key TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS observation_events_run ON events(run_id,seq);
CREATE INDEX IF NOT EXISTS observation_events_entity ON events(entity_id,run_id);
CREATE INDEX IF NOT EXISTS observation_events_pair ON events(run_id,trace_id,span_id,phase);
CREATE TABLE IF NOT EXISTS bodies(
 body_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
 size INTEGER NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS observation_bodies_run ON bodies(run_id);
"""


def checked_run_id(value: str) -> str:
    if not _RUN_ID.fullmatch(value):
        raise ValueError("Invalid observation run ID")
    return value


def decoded(raw: str) -> Any:
    result = load_bounded_observability_json(raw, max_bytes=CONTEXT_LIMIT)
    if not result.ok:
        raise GatewayStateError("Observation record is malformed")
    return result.value


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ObservationStore:
    def __init__(self, state_root: Path, *, writable: bool = False) -> None:
        self.anchor = Path(os.path.abspath(state_root))
        self.root = self.anchor / "observability"
        self.database = self.root / "index.sqlite3"
        self.writable = writable
        self.lock = threading.RLock()
        if writable:
            _ensure_private_root(self.root, trusted_anchor=self.anchor)
            _ensure_private_database_file(self.database)
            with self.connection(write=True) as connection:
                connection.executescript(_SCHEMA)
                row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if row and row[0] != "1":
                    raise GatewayStateError("Unsupported observation schema")
                connection.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write and not self.writable:
            raise GatewayStateError("Observation reader cannot write")
        _validate_private_root(self.root, trusted_anchor=self.anchor)
        _validate_sqlite_files(self.database)
        connection = sqlite3.connect(self.database.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                     uri=True, timeout=0.25)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            if write:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("PRAGMA query_only=ON")
                deadline = time.monotonic() + 2
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                connection.execute("BEGIN")
                row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if not row or row[0] != "1":
                    raise GatewayStateError("Unsupported observation schema")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()
            _validate_private_root(self.root, trusted_anchor=self.anchor)
            _validate_sqlite_files(self.database)

    def safe(self, payload: Any) -> Any:
        return bound_observability_payload(payload).value

    def put_configuration(self, payload: dict[str, Any]) -> str:
        bounded = bound_observability_payload(payload)
        safe = bounded.value
        if bounded.truncated:
            safe = {**safe, "capture_state": "truncated", "truncation_reasons": list(bounded.truncation_reasons)}
        key = fingerprint(safe)
        raw = encoded(safe)
        if len(raw.encode()) > CONTEXT_LIMIT:
            raise ValueError("Configuration snapshot exceeds limits")
        with self.lock, self.connection(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO configurations VALUES(?,?,?)", (key, raw, time.time()))
        return key

    def configuration(self, key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM configurations WHERE config_id=?", (key,)).fetchone()
        return decoded(row[0]) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        with self.lock, self.connection(write=True) as connection:
            connection.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, encoded(self.safe(value))))

    def meta(self, key: str) -> Any:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return decoded(row[0]) if row else None

    def project_run(self, run: dict[str, Any]) -> None:
        checked_run_id(run["run_id"])
        allowed = ("run_id", "state", "error_code", "created_at", "started_at", "finished_at",
                   "updated_at", "generation", "channel", "conversation_kind", "receipts", "outbox", "approvals")
        safe = self.safe({key: run.get(key) for key in allowed})
        for key in ("receipts", "outbox", "approvals"):
            safe[key] = encoded(safe[key] or [])
        with self.lock, self.connection(write=True) as connection:
            previous = connection.execute("SELECT state FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()
            connection.execute(
                f"INSERT INTO runs({','.join(allowed)}) VALUES({','.join('?' for _ in allowed)}) "
                "ON CONFLICT(run_id) DO UPDATE SET " + ",".join(f"{key}=excluded.{key}" for key in allowed[1:]),
                tuple(safe[key] for key in allowed),
            )
        if run["state"] in TERMINAL:
            with self.lock, self.connection(write=True) as connection:
                connection.execute("UPDATE runs SET capture_state='recorded' WHERE run_id=? AND capture_state='recording'", (run["run_id"],))
        if previous is None or previous[0] != run["state"]:
            self.append(run["run_id"], {"kind": "run_state", "layer": "gateway", "entity_id": "gateway:instance",
                        "status": run["state"], "created_at": run["updated_at"], "data": {"code": run.get("error_code")}})

    def bind_run(self, run_id: str, *, config_id: str, backend: str = "", model: str = "", role: str = "") -> None:
        with self.lock, self.connection(write=True) as connection:
            snapshot = connection.execute("SELECT payload FROM configurations WHERE config_id=?", (config_id,)).fetchone()
            revision = decoded(snapshot[0]).get("configuration_revision", config_id) if snapshot else config_id
            connection.execute("UPDATE runs SET config_id=COALESCE(config_id,?),config_revision=COALESCE(config_revision,?),"
                               "backend=CASE WHEN ?!='' THEN ? ELSE backend END,model=CASE WHEN ?!='' THEN ? ELSE model END,"
                               "role=CASE WHEN ?!='' THEN ? ELSE role END,capture_state=CASE WHEN capture_state='not_recorded' "
                               "THEN 'recording' ELSE capture_state END WHERE run_id=?",
                               (config_id, revision, backend, backend, model, model, role, role, run_id))

    def body(self, run_id: str, body_id: str) -> dict[str, Any] | None:
        checked_run_id(run_id)
        if not _BODY_ID.fullmatch(body_id):
            raise ValueError("Invalid observation detail ID")
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM bodies WHERE body_id=? AND run_id=?", (body_id, run_id)).fetchone()
            run = connection.execute("SELECT finished_at,state,config_revision FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        expired = bool(run and run["state"] in TERMINAL and run["finished_at"] is not None
                       and run["finished_at"] + RETENTION_SECONDS <= time.time())
        provenance = {"source": "observation_detail", "run_id": run_id, "recorded_at": row["created_at"],
                      "configuration_revision": run["config_revision"] if run else None,
                      "details_expires_at": run["finished_at"] + RETENTION_SECONDS if run and run["finished_at"] is not None else None}
        if row["state"] == "expired" or expired:
            return {**provenance, "body_id": body_id, "state": "expired", "payload": None}
        folder = self._body_directory(run_id)
        _validate_private_root(folder, trusted_anchor=self.root)
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(body_id + ".json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                _validate_private_file_metadata(info, folder / (body_id + ".json"))
                if info.st_size > CONTEXT_LIMIT or info.st_size != row["size"]:
                    raise GatewayStateError("Observation body size changed")
                payload = decoded(stream.read(CONTEXT_LIMIT + 1).decode("utf-8"))
        finally:
            os.close(directory)
        return {**provenance, "body_id": body_id, "state": row["state"], "payload": payload}

    def put_body(self, run_id: str, kind: str, payload: Any, *, limit: int = BODY_LIMIT,
                 capture_state: str | None = None) -> tuple[str | None, str]:
        checked_run_id(run_id)
        bounded = bound_observability_payload(payload)
        raw = encoded(bounded.value).encode()
        state = "truncated" if bounded.truncated else "available"
        if len(raw) > limit:
            raw = encoded({"preview": raw[:limit // 4].decode("utf-8", "ignore"), "truncated": True}).encode()
            state = "truncated"
        if capture_state == "capture_failed" or (state == "available" and capture_state in {"truncated", "not_recorded"}):
            state = capture_state
        directory = None
        body_id = None
        created = False
        try:
            with self.lock, self.connection(write=True) as connection:
                run = connection.execute("SELECT body_bytes,details_expired FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if run is None:
                    raise ValueError("Observation run is missing")
                if run["details_expired"]:
                    return None, "expired"
                if run["body_bytes"] + len(raw) > RUN_BODY_LIMIT:
                    connection.execute("UPDATE runs SET capture_state='truncated' WHERE run_id=? AND capture_state!='capture_failed'", (run_id,))
                    return None, "truncated"
                folder = self._body_directory(run_id)
                _ensure_private_root(folder, trusted_anchor=self.root)
                body_id = uuid.uuid4().hex
                directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptor = os.open(body_id + ".json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory)
                created = True
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                connection.execute("INSERT INTO bodies VALUES(?,?,?,?,?,?)", (body_id, run_id, kind, len(raw), state, time.time()))
                connection.execute("UPDATE runs SET body_bytes=body_bytes+? WHERE run_id=?", (len(raw), run_id))
        except BaseException:
            if created and directory is not None and body_id is not None:
                try:
                    os.unlink(body_id + ".json", dir_fd=directory)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if directory is not None:
                os.close(directory)
        return body_id, state

    def attach_body(self, run_id: str, kind: str, payload: Any) -> None:
        column = {"input": "input_ref", "result": "result_ref"}[kind]
        with self.lock:
            with self.connection() as connection:
                row = connection.execute(f"SELECT {column} FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if not row or row[0]:
                    return
            body_id, _ = self.put_body(run_id, kind, payload)
            with self.connection(write=True) as connection:
                connection.execute(f"UPDATE runs SET {column}=? WHERE run_id=?", (body_id, run_id))

    def append(self, run_id: str, event: dict[str, Any], *, body: Any = None, context: bool = False) -> int | None:
        checked_run_id(run_id)
        event = self.safe(event)
        data = event.get("data") or {}
        now = event.get("created_at", time.time())
        span = data.get("span_id") or event.get("span_id")
        trace = data.get("trace_id") or event.get("trace_id")
        phase = event.get("phase", "")
        identity = [run_id, event["kind"], trace, span]
        if phase == "update":
            identity.extend([phase, data.get("revision")])
        event_key = fingerprint(identity) if span and phase else None
        with self.lock:
            with self.connection() as connection:
                if event_key and connection.execute("SELECT 1 FROM events WHERE event_key=?", (event_key,)).fetchone():
                    return None
            body_ref, body_state = None, "not_recorded"
            if body is not None:
                try:
                    body_ref, body_state = self.put_body(run_id, event["kind"], body,
                        limit=CONTEXT_LIMIT if context else BODY_LIMIT, capture_state=event.get("body_state"))
                except (OSError, ValueError, sqlite3.Error, GatewayStateError):
                    body_state = "capture_failed"
            elif event.get("body_state") in {"not_recorded", "truncated", "capture_failed"}:
                body_state = event["body_state"]
            usage = data.get("usage") or {}
            def count(*names: str) -> int | None:
                return next((usage[key] for key in names if type(usage.get(key)) is int and usage[key] >= 0), None)
            with self.connection(write=True) as connection:
                elapsed = None
                if phase == "finish" and span and data.get("duration_recorded") is not False:
                    start = connection.execute("SELECT created_at FROM events WHERE run_id=? AND trace_id IS ? "
                                               "AND span_id=? AND phase='start' ORDER BY seq LIMIT 1", (run_id, trace, span)).fetchone()
                    if start:
                        elapsed = max(0, (now - start[0]) * 1000)
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO events(run_id,kind,layer,entity_id,refs,status,created_at,trace_id,span_id,parent_span_id,"
                    "phase,name,model,backend,error_code,elapsed_ms,input_tokens,output_tokens,cached_tokens,total_tokens,metadata,body_ref,body_state,event_key) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, event["kind"], event.get("layer", "gateway"), event.get("entity_id"), encoded(event.get("refs", [])),
                     event.get("status"), now, trace, span, data.get("parent_span_id"), phase, data.get("name"),
                     data.get("model"), data.get("backend"), data.get("code"), elapsed,
                     count("input_tokens", "prompt_tokens"), count("output_tokens", "completion_tokens"),
                     count("cached_tokens", "cache_read_tokens"), count("total_tokens"), encoded(data), body_ref, body_state, event_key),
                )
                if body_state in {"capture_failed", "truncated"}:
                    connection.execute("UPDATE runs SET capture_state=CASE WHEN capture_state='capture_failed' "
                                       "THEN capture_state ELSE ? END WHERE run_id=?", (body_state, run_id))
                return cursor.lastrowid

    def expire(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        cleaned = 0
        with self.lock, self.connection(write=True) as connection:
            runs = connection.execute("SELECT run_id FROM runs WHERE state IN ('completed','failed','aborted') "
                                      "AND finished_at<=? AND details_expired=0 LIMIT 100", (current - RETENTION_SECONDS,)).fetchall()
            for run in runs:
                run_id = run[0]
                rows = connection.execute("SELECT body_id FROM bodies WHERE run_id=? AND state!='expired'", (run_id,)).fetchall()
                folder = self._body_directory(run_id)
                if rows:
                    _validate_private_root(folder, trusted_anchor=self.root)
                    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        checked = []
                        for row in rows:
                            filename = row[0] + ".json"
                            try:
                                info = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                                _validate_private_file_metadata(info, folder / filename)
                                checked.append((filename, info.st_dev, info.st_ino))
                            except FileNotFoundError:
                                pass
                        for filename, device, inode in checked:
                            info = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                            if (info.st_dev, info.st_ino) != (device, inode):
                                raise GatewayStateError("Observation detail changed during cleanup")
                            os.unlink(filename, dir_fd=directory)
                    finally:
                        os.close(directory)
                connection.execute("UPDATE bodies SET state='expired' WHERE run_id=?", (run_id,))
                connection.execute("UPDATE events SET body_state='expired' WHERE run_id=? AND body_ref IS NOT NULL", (run_id,))
                connection.execute("UPDATE runs SET details_expired=1,body_bytes=0 WHERE run_id=?", (run_id,))
                cleaned += 1
        return cleaned

    def _body_directory(self, run_id: str) -> Path:
        return self.root / "details" / hashlib.sha256(run_id.encode()).hexdigest()
