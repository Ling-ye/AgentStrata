from __future__ import annotations

from contextlib import contextmanager
import errno
import multiprocessing
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from chatcopilot.core.private_sqlite import PrivateDatabase, private_file


SCHEMA = "CREATE TABLE IF NOT EXISTS counters (id INTEGER PRIMARY KEY, value INTEGER NOT NULL);"
TASK_ID = "repair-" + "a" * 32


@pytest.fixture
def database(tmp_path):
    db = PrivateDatabase(tmp_path / "private" / "state.sqlite3", SCHEMA)
    with db.connect(write=True) as connection:
        connection.execute("INSERT INTO counters VALUES(1,0)")
    return db


@contextmanager
def sqlite_commit_at_stat(monkeypatch, db, suffix="-journal", *, after_stat=False,
                          unlinked_stat=False, sql="PRAGMA user_version=1"):
    """End a real SQLite transaction/connection at the sidecar inspection boundary."""
    writer = sqlite3.connect(db.path)
    auxiliary = Path(str(db.path) + suffix)
    if suffix != "-journal":
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(sql)
    if suffix != "-journal":
        writer.commit()
    assert auxiliary.exists()
    original = Path.lstat
    observed = []

    def inspect(path, *args, **kwargs):
        if path == auxiliary and not observed:
            observed.append(suffix)
            info = original(path, *args, **kwargs) if after_stat else None
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW) if unlinked_stat else None
            try:
                if suffix == "-journal":
                    writer.commit()
                else:
                    writer.close()
                assert not auxiliary.exists()
                if fd is not None:
                    # Keep the kernel inode reference that an in-flight stat holds.
                    info = os.fstat(fd)
                    assert info.st_nlink == 0
                if info is not None:
                    return info
            finally:
                if fd is not None:
                    os.close(fd)
        return original(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "lstat", inspect)
            yield observed
    finally:
        writer.close()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("after_stat", [False, True])
def test_sqlite_can_remove_sidecar_during_inspection(database, monkeypatch, suffix, write, after_stat):
    calls = []
    with sqlite_commit_at_stat(monkeypatch, database, suffix, after_stat=after_stat,
                               sql="UPDATE counters SET value=value+1 WHERE id=1") as observed:
        with database.connect(write=write) as connection:
            calls.append(True)
            assert connection.execute("SELECT value FROM counters").fetchone()[0] == 1
            if write:
                connection.execute("UPDATE counters SET value=value+1 WHERE id=1")
    assert observed == [suffix] and len(calls) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT value FROM counters").fetchone()[0] == 1 + int(write)
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("unsafe", ["symlink", "dangling_symlink", "directory", "hardlink", "mode", "owner"])
def test_existing_sidecar_still_requires_private_regular_file(database, monkeypatch, tmp_path, suffix, unsafe):
    sidecar = Path(str(database.path) + suffix)
    target = tmp_path / "outside"
    target.write_bytes(b"preserve")
    target.chmod(0o600)
    if unsafe in {"symlink", "dangling_symlink"}:
        sidecar.symlink_to(target if unsafe == "symlink" else tmp_path / "missing")
    elif unsafe == "directory":
        sidecar.mkdir(mode=0o700)
    elif unsafe == "hardlink":
        os.link(target, sidecar)
    else:
        sidecar.touch(mode=0o600)
        if unsafe == "mode":
            sidecar.chmod(0o640)
        else:
            original = Path.lstat
            def other_owner(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == sidecar:
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1, st_nlink=info.st_nlink)
                return info
            monkeypatch.setattr(Path, "lstat", other_owner)
    with pytest.raises(ValueError, match="private storage file"):
        with database.connect():
            pytest.fail("unsafe sidecar reached the transaction")
    assert target.read_bytes() == b"preserve"


@pytest.mark.parametrize("failure", [PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "io failure")])
def test_other_sidecar_io_errors_are_not_ignored(database, monkeypatch, failure):
    sidecar = Path(str(database.path) + "-journal")
    sidecar.touch(mode=0o600)
    original = Path.lstat
    def fail(path, *args, **kwargs):
        if path == sidecar:
            raise failure
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(type(failure)) as caught:
        with database.connect():
            pytest.fail("failed inspection reached the transaction")
    assert caught.value is failure


def test_missing_main_database_is_not_optional(database):
    database.path.unlink()
    with pytest.raises(FileNotFoundError):
        with database.connect():
            pytest.fail("missing database reached the transaction")
    assert not database.path.exists()
    with pytest.raises(FileNotFoundError):
        private_file(database.path)


def test_database_replacement_is_rejected(database, monkeypatch):
    replacement = PrivateDatabase(database.path.with_name("replacement.sqlite3"), SCHEMA)
    original = sqlite3.connect
    def replace_while_opening(*args, **kwargs):
        connection = original(*args, **kwargs)
        os.replace(replacement.path, database.path)
        return connection
    monkeypatch.setattr(sqlite3, "connect", replace_while_opening)
    with pytest.raises(ValueError, match="identity changed"):
        with database.connect():
            pytest.fail("replacement reached the transaction")


def test_body_file_error_rolls_back_and_is_not_replayed(database, tmp_path):
    error = FileNotFoundError(errno.ENOENT, "artifact missing", str(tmp_path / "artifact"))
    calls = []
    with pytest.raises(FileNotFoundError) as caught:
        with database.connect(write=True) as connection:
            calls.append(True)
            connection.execute("UPDATE counters SET value=99")
            raise error
    assert caught.value is error and len(calls) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT value FROM counters").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE counters SET value=1")


def test_corrupt_database_is_not_ignored(database):
    database.path.write_bytes(b"invalid sqlite contents")
    with pytest.raises(sqlite3.DatabaseError):
        with database.connect():
            pytest.fail("corrupt database reached the transaction")


def _store_process(root, role, iterations, ready, start, results):
    from chatcopilot.harness.store import HarnessStore
    try:
        store = HarnessStore(Path(root))
        ready.set()
        assert start.wait(15)
        for number in range(iterations):
            # Console constructs its controller/store per request as well as polling it.
            if number % 10 == 0:
                store = HarnessStore(Path(root))
            task = store.get(TASK_ID)
            if role == "writer":
                store.update(TASK_ID, counter=task.get("counter", 0) + 1, heartbeat_at=number)
            else:
                assert task["task_id"] == TASK_ID
                assert len(store.page(page=1, limit=20, search="", status="")["tasks"]) == 1
        results.put((role, iterations, None))
    except BaseException as error:
        ready.set()
        results.put((role, 0, f"{type(error).__name__}: {error}"))


def test_harness_multiprocess_heartbeats_and_polling_keep_all_updates(tmp_path):
    from chatcopilot.harness.store import HarnessStore
    root = tmp_path / "harness"
    store = HarnessStore(root)
    store.create({"task_id": TASK_ID, "request_key": "request", "request_digest": "digest",
                  "match_key": "match", "context_key": "context", "active_key": "active", "counter": 0})
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    processes = []
    iterations = 200
    try:
        for role in ("writer", "detail-reader", "history-reader"):
            ready = context.Event()
            process = context.Process(target=_store_process,
                args=(str(root), role, iterations, ready, start, results))
            process.start()
            processes.append(process)
            assert ready.wait(15)
        start.set()
        received = [results.get(timeout=30) for _ in processes]
        assert all(count == iterations and error is None for _, count, error in received), received
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert store.get(TASK_ID)["counter"] == iterations
        assert len(store.history()) == 1
        with store.database.connect() as connection:
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("write", [False, True])
def test_sqlite_can_report_unlinked_sidecar_inode(database, monkeypatch, suffix, write):
    with sqlite_commit_at_stat(monkeypatch, database, suffix, unlinked_stat=True,
                               sql="UPDATE counters SET value=value+1 WHERE id=1") as observed:
        with database.connect(write=write) as connection:
            assert connection.execute("SELECT value FROM counters").fetchone()[0] == 1
            if write:
                connection.execute("UPDATE counters SET value=value+1 WHERE id=1")
    assert observed == [suffix]
    with database.connect() as connection:
        assert connection.execute("SELECT value FROM counters").fetchone()[0] == 1 + int(write)
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("unsafe", ["type", "owner", "mode"])
def test_unlinked_sidecar_keeps_type_owner_and_mode_checks(database, monkeypatch, unsafe):
    import stat
    sidecar = Path(str(database.path) + "-journal")
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid(), st_nlink=0)
    if unsafe == "type":
        info.st_mode = stat.S_IFLNK | 0o600
    elif unsafe == "owner":
        info.st_uid += 1
    else:
        info.st_mode |= 0o040
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: info if path == sidecar else original(path, *a, **kw))
    with pytest.raises(ValueError, match="private storage file"):
        with database.connect():
            pytest.fail("unsafe unlinked metadata reached SQLite")


def test_main_database_still_rejects_zero_links(database, monkeypatch):
    fd = os.open(database.path, os.O_RDONLY)
    try:
        database.path.unlink()
        info = os.fstat(fd)
        assert info.st_nlink == 0
    finally:
        os.close(fd)
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: info if path == database.path else original(path, *a, **kw))
    with pytest.raises(ValueError, match="private storage file"):
        with database.connect():
            pytest.fail("unlinked main database reached SQLite")
