"""Private local SQLite storage shared by independent application owners."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


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


def private_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise ValueError("private storage file requires current ownership, mode 0600 and one link")
    return info


def json_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


class PrivateDatabase:
    def __init__(self, path: Path, schema: str) -> None:
        self.path = path.absolute()
        private_directory(self.path.parent)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.connect(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported database schema version")
            # Schema statements are fixed application text, never supplied by callers.
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        private_directory(self.path.parent)
        before = private_file(self.path)
        for suffix in ("-journal", "-wal", "-shm"):
            auxiliary = Path(str(self.path) + suffix)
            if auxiliary.exists() or auxiliary.is_symlink():
                private_file(auxiliary)
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            after = private_file(self.path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("database identity changed while opening")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
