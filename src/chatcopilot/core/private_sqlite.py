"""Private local SQLite storage shared by independent application owners."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


logger = logging.getLogger(__name__)


@contextmanager
def private_lock(path: Path, *, exclusive: bool = True, timeout: float | None = 0) -> Iterator[int]:
    """Lock a stable private inode; timeout=None waits until its owner releases it."""
    import fcntl

    private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        private_file(path)
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        _require_private_file(opened)
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise ValueError("private lock requires mode 0600")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(descriptor, flags if timeout is None else flags | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is None or timeout == 0:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("private storage initialization lock timed out") from None
                time.sleep(min(0.05, remaining))
        current = private_file(path)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("private lock identity changed while opening")
        yield descriptor
    finally:
        # Keeping the pathname is essential: unlinking would permit two lock inodes.
        os.close(descriptor)


def storage_error_details(error: BaseException) -> dict[str, Any] | None:
    return getattr(error, "storage_details", None)


def _record_error(error: BaseException, database: str, phase: str) -> None:
    details = {"database": database, "phase": phase, "type": type(error).__name__,
               "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
               "sqlite_errorname": getattr(error, "sqlite_errorname", None)}
    if storage_error_details(error) is None:
        error.storage_details = details
    # No SQL text, parameters, exception message or frame locals in storage diagnostics.
    frames = "".join(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
                     for frame in traceback.extract_tb(error.__traceback__))
    logger.error("Private storage failure %s\n%s", json_text(details), frames)


def private_directory(path: Path) -> Path:
    path = path.absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("private storage path contains a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private storage directory must be owned by this user with mode 0700")
    return path


def repair_owned_private_directory(path: Path) -> Path:
    """Create or tighten one application-owned directory without relaxing trust checks."""
    path = path.absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("private storage path contains a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid():
            raise ValueError("private storage directory must be owned by this user with mode 0700")
        if stat.S_IMODE(opened.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
        tightened = os.fstat(descriptor)
        linked = path.lstat()
        if (
            stat.S_IMODE(tightened.st_mode) != 0o700
            or (tightened.st_dev, tightened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError("private storage directory must be owned by this user with mode 0700")
    finally:
        os.close(descriptor)
    return path


def _require_private_file(info: os.stat_result, *, allow_unlinked: bool = False) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink not in ((0, 1) if allow_unlinked else (1,))
        or info.st_mode & 0o077
    ):
        raise ValueError("private storage file requires current ownership, mode 0600 and one link")


def private_file(path: Path) -> os.stat_result:
    info = path.lstat()
    _require_private_file(info)
    return info


def json_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


class PrivateDatabase:
    def __init__(self, path: Path, schema: str) -> None:
        self.path = path.absolute()
        try:
            self._initialize(schema)
        except (sqlite3.Error, OSError) as error:
            if storage_error_details(error) is None:
                _record_error(error, self.path.name, "initialize")
            raise

    def _initialize(self, schema: str) -> None:
        # Initialization is a trusted one-time critical section. The kernel
        # releases the advisory lock if its owner dies, so waiting avoids a
        # false storage failure while another process is legitimately finishing
        # a transaction during concurrent startup.
        with private_lock(self.path.with_name(self.path.name + ".init.lock"), timeout=None):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                # Never open/close an existing DB outside SQLite: close() would drop
                # every POSIX SQLite lock this process holds on that inode.
                pass
            else:
                os.close(fd)  # All constructors hold the init lock before SQLite opens.
            with self.connect() as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return
            if version != 0:
                raise ValueError("unsupported database schema version")
            with self.connect(write=True) as connection:
                # Fixed application text; initialized stores never repeat DDL/writes.
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        failure = None
        phase = "open"
        try:
            connection = self._open()
            phase = "begin"
            if not write:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            phase = "transaction"
            yield connection
            if write:
                phase = "commit"
                connection.commit()
        except BaseException as error:
            failure = error
            if isinstance(error, sqlite3.Error) or (phase != "transaction" and isinstance(error, OSError)):
                _record_error(error, self.path.name, phase)
            if connection is not None:
                try:
                    connection.rollback()
                except Exception as secondary:
                    _record_error(secondary, self.path.name, "rollback")
            raise
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as error:
                    _record_error(error, self.path.name, "close")
                    if failure is None:
                        raise

    def _open(self) -> sqlite3.Connection:
        private_directory(self.path.parent)
        before = private_file(self.path)
        for suffix in ("-journal", "-wal", "-shm"):
            auxiliary = Path(str(self.path) + suffix)
            try:
                # On tmpfs, stat may retain an inode reference while SQLite unlinks
                # its sidecar and return nlink=0 instead of ENOENT. The removed inode
                # must still be a private regular file owned by this user. Main DBs
                # and all other private_file callers continue to require one link.
                _require_private_file(auxiliary.lstat(), allow_unlinked=True)
            except FileNotFoundError:
                # SQLite can unlink these files when another connection commits
                # or closes. Only their absence is optional, not their safety.
                pass
        # Independent Console/worker processes legitimately serialize on the
        # same private DB. Give SQLite enough time to hand the writer lease over
        # under the repository's documented stress envelope.
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            after = private_file(self.path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("database identity changed while opening")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except BaseException:
            try:
                connection.close()
            except Exception as secondary:
                _record_error(secondary, self.path.name, "close")
            raise
