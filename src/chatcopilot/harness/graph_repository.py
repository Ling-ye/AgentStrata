"""Private official SQLite checkpoints, separate from the Harness business database."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from chatcopilot.core.private_sqlite import private_directory, private_file, private_lock


@contextmanager
def checkpoints(root: Path, identity: str):
    directory = private_directory(root / "graphs")
    key = hashlib.sha256(identity.encode()).hexdigest()
    path = directory / (key + ".sqlite3")
    # A graph has one executor, including across independent timer invocations.
    with private_lock(directory / (key + ".lock"), timeout=5):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        original = private_file(path)
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                private_file(Path(str(path) + suffix))
            except FileNotFoundError:
                pass
        connection = sqlite3.connect(
            path.as_uri() + "?mode=rw", uri=True, check_same_thread=False, timeout=5
        )
        try:
            current = private_file(path)
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("graph checkpoint database identity changed")
            # State contains only plain data; never deserialize arbitrary classes or pickle.
            saver = SqliteSaver(
                connection,
                serde=JsonPlusSerializer(allowed_msgpack_modules=[], allowed_json_modules=[]),
            )
            connection.execute("PRAGMA synchronous=FULL")
            saver.setup()
            yield saver
        finally:
            connection.close()
