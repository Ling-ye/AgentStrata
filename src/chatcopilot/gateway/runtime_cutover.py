"""Phase-A Gateway schema cutover under its singleton lease."""

from __future__ import annotations
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4
from contextlib import closing, contextmanager
from chatcopilot.gateway.interaction_schema import INTERACTION_SCHEMA
from chatcopilot.gateway.state_store import (
    _acquire_instance_lease,
    _INSTANCE_LEASE_FILENAME,
    _validate_private_root,
    _validate_sqlite_files,
)


def migrate_database(root: Path, *, apply: bool = False, held_session=None) -> dict:
    root = root.absolute()
    _validate_private_root(root, trusted_anchor=root.parent)
    database = root / "gateway.sqlite3"
    _validate_sqlite_files(database)
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        row = connection.execute(
            "SELECT value FROM gateway_meta WHERE key='schema_version'"
        ).fetchone()
    if row is None or row[0] not in {"2", "3"}:
        raise ValueError("unsupported Gateway schema")
    result = {"schema_before": int(row[0]), "schema_after": 3, "applied": False}
    if not apply or row[0] == "3":
        return result
    if held_session is not None and (held_session[0] != root or not held_session[1].held):
        raise ValueError("migration lease does not match state root")
    lease = (
        held_session[1]
        if held_session
        else _acquire_instance_lease(root / _INSTANCE_LEASE_FILENAME)
    )
    try:
        backup = root / ("gateway.pre-runtime-" + uuid4().hex + ".sqlite3")
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as connection:
            with closing(sqlite3.connect(backup)) as target:
                connection.backup(target)
            approval_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='approvals'"
            ).fetchone()[0]
            upgrade = ""
            if "'cancelled'" not in approval_sql:
                replacement = approval_sql.replace(
                    "('pending', 'resolved', 'expired')",
                    "('pending', 'resolved', 'expired', 'cancelled')",
                )
                replacement = replacement.replace(
                    "state = 'expired'", "state IN ('expired', 'cancelled')"
                )
                if replacement == approval_sql:
                    raise ValueError("unrecognized approval schema; migration refused")
                upgrade = (
                    "ALTER TABLE approvals RENAME TO approvals_pre_runtime;\n" + replacement + ";\n"
                    "INSERT INTO approvals SELECT * FROM approvals_pre_runtime;\nDROP TABLE approvals_pre_runtime;\n"
                )
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + upgrade
                + INTERACTION_SCHEMA
                + "\nUPDATE gateway_meta SET value='3' WHERE key='schema_version';\nCOMMIT;"
            )
        return {**result, "applied": True, "backup_name": backup.name}
    finally:
        if held_session is None:
            lease.close()


def backup_database(root: Path, *, held_session) -> str:
    """Create an owner-only SQLite backup while the caller holds the instance lease."""

    root = root.absolute()
    if held_session[0] != root or not held_session[1].held:
        raise ValueError("cutover lease does not match state root")
    _validate_private_root(root, trusted_anchor=root.parent)
    database = root / "gateway.sqlite3"
    _validate_sqlite_files(database)
    backup = root / ("gateway.pre-runtime-" + uuid4().hex + ".sqlite3")
    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
        return backup.name
    except BaseException:
        backup.unlink(missing_ok=True)
        raise


def acquire_migration_lease(root: Path):
    _validate_private_root(root, trusted_anchor=root.parent)
    return _acquire_instance_lease(root / _INSTANCE_LEASE_FILENAME)


def check_migration_lease(root: Path) -> None:
    """Prove an existing instance lease is available without creating files."""

    import fcntl

    root = root.absolute()
    _validate_private_root(root, trusted_anchor=root.parent)
    path = root / _INSTANCE_LEASE_FILENAME
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Gateway instance lease is not a private ordinary file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Gateway instance lease is held; stop the instance first") from exc
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def migration_session(root: Path):
    root = root.absolute()
    lease = acquire_migration_lease(root)
    try:
        yield root, lease
    finally:
        lease.close()
