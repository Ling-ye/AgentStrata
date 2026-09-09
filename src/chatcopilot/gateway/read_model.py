"""Read-only, bounded Gateway diagnostics without constructing a runtime or writer."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Any, Iterator

from chatcopilot.core.observability_redaction import load_bounded_observability_json, redact_observability_payload
from .state_store import (
    GatewayStateError, SCHEMA_VERSION, _validate_private_root, _validate_sqlite_files,
    _validate_private_file_metadata,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def _file_identity(path: Path) -> tuple[int, ...] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _validate_private_file_metadata(info, path)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _snapshot_database(root: Path) -> Iterator[Path]:
    """Copy a stable DB/WAL pair; SQLite sidecars may only be created in this private copy."""
    names = ("gateway.sqlite3", "gateway.sqlite3-wal", "gateway.sqlite3-journal")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        before = {name: _file_identity(root / name) for name in names}
        if before[names[0]] is None:
            raise GatewayStateError("Gateway database is missing")
        if sum(identity[2] for identity in before.values() if identity) > _MAX_SNAPSHOT_BYTES:
            raise GatewayStateError("Gateway observation snapshot exceeds the byte limit")
        with tempfile.TemporaryDirectory(prefix="agentstrata-observation-") as temporary:
            target = Path(temporary)
            for name, identity in before.items():
                if identity is None:
                    continue
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                with os.fdopen(descriptor, "rb") as source:
                    info = os.fstat(source.fileno())
                    _validate_private_file_metadata(info, root / name)
                    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != identity:
                        raise GatewayStateError("Gateway state changed during snapshot")
                    output_fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(output_fd, "wb") as output:
                        remaining = identity[2]
                        while remaining:
                            block = source.read(min(remaining, 64 * 1024))
                            if not block:
                                raise GatewayStateError("Gateway snapshot was interrupted")
                            output.write(block)
                            remaining -= len(block)
            _validate_private_root(root, trusted_anchor=root.parent)
            _validate_sqlite_files(root / names[0])
            after = {name: _file_identity(root / name) for name in names}
            opened, current = os.fstat(directory_fd), root.stat()
            if before != after or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise GatewayStateError("Gateway state changed during snapshot; retry observation")
            yield target / names[0]
    finally:
        os.close(directory_fd)


@contextmanager
def _reader(root: Path) -> Iterator[sqlite3.Connection]:
    root = Path(os.path.abspath(root))
    _validate_private_root(root, trusted_anchor=root.parent)
    database = root / "gateway.sqlite3"
    _validate_sqlite_files(database)
    with _snapshot_database(root) as snapshot:
        connection = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 2
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("BEGIN")
            version = connection.execute("SELECT value FROM gateway_meta WHERE key='schema_version'").fetchone()
            if version is None or int(version[0]) != SCHEMA_VERSION:
                raise GatewayStateError("Unsupported Gateway state schema")
            yield connection
        finally:
            connection.close()


def _rows(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params)]


def _object(raw: str | None) -> dict[str, Any]:
    loaded = load_bounded_observability_json(raw or "{}", max_bytes=1024 * 1024)
    if not loaded.ok or not isinstance(loaded.value, dict):
        raise GatewayStateError("Gateway diagnostic record is malformed or exceeds limits")
    return loaded.value


def _metadata(data: Any, secrets: tuple[str, ...], *, operator: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in ("model", "backend", "name", "trace_id", "span_id", "parent_span_id", "coverage", "code", "gate", "outcome",
                "outbound_id", "receipt_id", "stage", "runtime_layer", "operation", "stage_span_id",
                "source", "target", "entrypoint", "run_state"):
        if isinstance(data.get(key), str):
            safe[key] = (data[key] if operator else redact_observability_payload(data[key], secrets=secrets).value)[:160]
    for key in ("iteration", "depth", "input_message_count", "input_estimated_tokens", "tool_schema_count", "message_count", "resource_count", "flow_version"):
        value = data.get(key)
        if type(value) is int and 0 <= value <= 10**12:
            safe[key] = value
    if type(data.get("duration_recorded")) is bool:
        safe["duration_recorded"] = data["duration_recorded"]
    if isinstance(data.get("usage"), dict):
        safe["usage"] = {key: value for key, value in data["usage"].items()
                         if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
                         and type(value) is int and 0 <= value <= 10**12}
    return safe


def gateway_runs(root: Path) -> dict[str, Any]:
    with _reader(root) as connection:
        runs = _rows(connection,
                     "SELECT r.run_id, r.state, r.error_code, r.created_at, r.started_at, "
                     "r.finished_at, r.updated_at, s.channel, s.conversation_kind "
                     "FROM runs r JOIN sessions s ON s.session_id=r.session_id "
                     "ORDER BY r.created_at DESC, r.run_id DESC LIMIT 51")
        audit = _rows(connection,
                      "SELECT allowed, code, policy_version, observed_at FROM authorization_decisions "
                      "ORDER BY observed_at DESC, decision_id DESC LIMIT 101")
        summary = dict(connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(state IN ('accepted','running','abort_requested','recovery_required')) AS active, "
            "SUM(state IN ('failed','aborted') AND updated_at >= ?) AS failed_recent "
            "FROM runs", (time.time() - 86400,)
        ).fetchone())
        return {"runs": runs[:50], "truncated": len(runs) > 50, "summary": summary,
                "audit": audit[:100], "audit_truncated": len(audit) > 100,
                "generated_at": time.time()}


def gateway_run(root: Path, run_id: str, *, secrets: tuple[str, ...] = (), operator: bool = False) -> dict[str, Any] | None:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid Gateway run ID")
    with _reader(root) as connection:
        row = connection.execute(
            "SELECT run_id, session_id, state, generation, created_at, started_at, finished_at, "
            "updated_at, error_code, substr(result_json,1,1048577) AS result_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        run = dict(row)
        result = _object(run.pop("result_json"))
        final_text = str(result.get("final_text") or "")
        if not operator:
            final_text = redact_observability_payload(final_text, secrets=secrets).value
        run["final_text"] = final_text[:8000]
        run["final_text_truncated"] = len(final_text) > 8000
        session_id = run.pop("session_id")
        approvals = _rows(connection,
                          "SELECT operation, state, accepted, created_at, decided_at "
                          "FROM approvals WHERE run_id=? AND session_id=? ORDER BY created_at LIMIT 101",
                          (run_id, session_id))
        receipts = _rows(connection,
                         "SELECT d.receipt_id, d.outbound_id, d.stage, d.observed_at, d.error_code "
                         "FROM delivery_receipts d JOIN outbox o ON o.outbound_id=d.outbound_id "
                         "WHERE o.run_id=? AND o.session_id=? ORDER BY d.rowid LIMIT 101",
                         (run_id, session_id))
        outbox = _rows(connection,
                       "SELECT outbound_id, state, error_code, created_at, updated_at FROM outbox "
                       "WHERE run_id=? AND session_id=? ORDER BY created_at LIMIT 101", (run_id, session_id))
        has_observations = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_observations'"
        ).fetchone() is not None
        observations = _rows(connection,
                             "SELECT seq, substr(payload_json,1,131072) AS payload_json, created_at "
                             "FROM run_observations WHERE run_id=? ORDER BY seq DESC LIMIT 301",
                             (run_id,)) if has_observations else []
        events = []
        for item in reversed(observations[:300]):
            payload = _object(item.pop("payload_json"))
            # Only the diagnostic schema is public; arbitrary stored keys never escape.
            events.append({**item, **{key: str(payload[key])[:160]
                for key in ("kind", "source", "target", "status", "phase", "trace_id", "span_id", "layer", "entity_id", "body_state") if key in payload},
                "data": _metadata(payload.get("data"), secrets, operator=operator)})
        wire = _rows(connection,
                     "SELECT seq, event, created_at, json_object("
                     "'stop_reason',json_extract(payload_json,'$.stop_reason'),"
                     "'code',json_extract(payload_json,'$.code'),"
                     "'retryable',json_extract(payload_json,'$.retryable'),"
                     "'stage',json_extract(payload_json,'$.stage')) AS payload_json "
                     "FROM gateway_events WHERE json_extract(payload_json,'$.run_id')=? "
                     "AND json_extract(payload_json,'$.session_id')=? ORDER BY seq DESC LIMIT 301",
                     (run_id, session_id))
        wire_events = []
        for item in reversed(wire[:300]):
            payload = _object(item.pop("payload_json"))
            wire_events.append({**item, "data": {
                key: payload[key] for key in ("stop_reason", "code", "retryable", "stage") if key in payload
            }})
        return {"run": run, "observations": events, "events": wire_events,
                "observations_available": has_observations and bool(observations),
                "truncated": len(observations) > 300 or len(wire) > 300 or any(
                    len(items) > 100 for items in (approvals, receipts, outbox)
                ) or any(item.get("kind") == "observations_truncated" for item in events),
                "approvals": approvals[:100], "receipts": receipts[:100], "outbox": outbox[:100],
                "generated_at": time.time()}
