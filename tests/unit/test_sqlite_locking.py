"""Real process/descriptor regressions for SQLite's process-owned POSIX locks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from chatcopilot.core.private_sqlite import PrivateDatabase, private_lock, storage_error_details


SCHEMA = "CREATE TABLE probe(value INTEGER); INSERT INTO probe VALUES(0);"
TASK = "repair-" + "a" * 32


def competitor(path):
    result = subprocess.run([sys.executable, "-c", """
import json, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0)
try:
    connection.execute('BEGIN IMMEDIATE')
    connection.execute('UPDATE probe SET value=value+10')
    connection.commit()
    print(json.dumps({'committed': True}))
except sqlite3.Error as error:
    print(json.dumps({'code': error.sqlite_errorcode}))
finally:
    connection.close()
""", str(path)], capture_output=True, text=True, check=True, timeout=10)
    return json.loads(result.stdout)


@pytest.mark.parametrize("operation", ["constructor", "evaluation", "creation", "maintenance"])
def test_other_process_stays_locked_during_same_process_store_operations(tmp_path, operation):
    from chatcopilot.harness.store import HarnessStore
    from chatcopilot.evals.application.result_store import EvaluationResultStore

    factory = EvaluationResultStore if operation == "evaluation" else HarnessStore
    root = tmp_path / "private"
    store = factory(root)
    with store.database.connect(write=True) as connection:
        connection.executescript(SCHEMA)
    with store.database.connect(write=True) as writer:
        writer.execute("UPDATE probe SET value=1")
        for _ in range(3):
            if operation in {"constructor", "evaluation"}:
                factory(root)
                guard = nullcontext()
            else:
                guard = store.creation_guard() if operation == "creation" else store.maintenance()
            with guard:
                assert competitor(store.database.path) == {"code": sqlite3.SQLITE_BUSY}
            # The bug happens on close at guard exit, so also probe AFTER it closes.
            assert competitor(store.database.path) == {"code": sqlite3.SQLITE_BUSY}
    assert competitor(store.database.path) == {"committed": True}
    with store.database.connect() as connection:
        assert connection.execute("SELECT value FROM probe").fetchone()[0] == 11
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _initialize(path, ready, results):
    try:
        assert ready.wait(10)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: PrivateDatabase(Path(path), SCHEMA), range(8)))
        results.put(None)
    except BaseException as error:
        results.put(repr(error))


def test_first_initialization_is_atomic_across_threads_and_processes(tmp_path):
    path = tmp_path / "private" / "state.sqlite3"
    context = multiprocessing.get_context("spawn")
    ready, results = context.Event(), context.Queue()
    processes = [context.Process(target=_initialize, args=(str(path), ready, results)) for _ in range(4)]
    try:
        for process in processes:
            process.start()
        ready.set()
        assert [results.get(timeout=30) for _ in processes] == [None] * len(processes)
        for process in processes:
            process.join(5)
            assert process.exitcode == 0
        database = PrivateDatabase(path, SCHEMA)
        with database.connect() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM probe").fetchone()[0] == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        results.close()
        results.join_thread()


def test_warm_constructor_does_not_write_or_repair_an_initialized_schema(tmp_path):
    path = tmp_path / "private" / "state.sqlite3"
    database = PrivateDatabase(path, SCHEMA)
    before = path.stat().st_mtime_ns
    PrivateDatabase(path, "INVALID SQL MUST NOT EXECUTE")
    assert path.stat().st_mtime_ns == before
    with database.connect(write=True) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError, match="unsupported"):
        PrivateDatabase(path, SCHEMA)


@pytest.mark.parametrize("name", ["state.sqlite3.init.lock", "harness.lock"])
@pytest.mark.parametrize("unsafe", ["symlink", "dangling", "hardlink", "mode", "readonly", "directory"])
def test_lock_rejects_unsafe_existing_files(tmp_path, name, unsafe):
    path = tmp_path / name
    target = tmp_path / "target"
    target.write_bytes(b"preserve")
    target.chmod(0o600)
    if unsafe in {"symlink", "dangling"}:
        path.symlink_to(target if unsafe == "symlink" else tmp_path / "missing")
    elif unsafe == "hardlink":
        os.link(target, path)
    elif unsafe == "directory":
        path.mkdir(mode=0o700)
    else:
        path.touch(mode=0o600)
        path.chmod(0o640 if unsafe == "mode" else 0o400)
    with pytest.raises((ValueError, OSError)):
        with private_lock(path):
            pytest.fail("unsafe lock accepted")
    assert target.read_bytes() == b"preserve"


def test_lock_identity_timeout_and_stability(tmp_path, monkeypatch):
    import fcntl

    path = tmp_path / "state.init.lock"
    with private_lock(path):
        identity = path.stat().st_ino
        with pytest.raises(TimeoutError, match="timed out"):
            with private_lock(path, timeout=0.02):
                pytest.fail("exclusive lock bypassed")
    assert path.stat().st_ino == identity
    flock = fcntl.flock
    def replace(fd, flags):
        flock(fd, flags)
        path.rename(tmp_path / "previous.lock")
        path.touch(mode=0o600)
    monkeypatch.setattr(fcntl, "flock", replace)
    with pytest.raises(ValueError, match="identity changed"):
        with private_lock(path):
            pytest.fail("replaced lock accepted")


def test_lock_rejects_wrong_owner(tmp_path, monkeypatch):
    path = tmp_path / "private.lock"
    path.touch(mode=0o600)
    original = Path.lstat
    def other_owner(current, *args, **kwargs):
        info = original(current, *args, **kwargs)
        if current == path:
            fields = list(info)
            fields[4] += 1
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(Path, "lstat", other_owner)
    with pytest.raises(ValueError, match="ownership"):
        with private_lock(path):
            pytest.fail("wrong owner accepted")


@pytest.mark.parametrize("phase", ["connect", "begin", "body", "commit"])
@pytest.mark.parametrize("code,name", [(5898, "SQLITE_IOERR_DELETE_NOENT"), (13, "SQLITE_FULL"), (8, "SQLITE_READONLY")])
def test_storage_error_keeps_original_details_without_replaying(tmp_path, monkeypatch, caplog, phase, code, name):
    path = tmp_path / "private" / "state.sqlite3"
    database = PrivateDatabase(path, SCHEMA)
    original = sqlite3.connect
    error = sqlite3.OperationalError("PRIVATE SQL PARAMETER")
    error.sqlite_errorcode, error.sqlite_errorname = code, name
    calls = []
    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if phase == "begin" and sql == "BEGIN IMMEDIATE":
                raise error
            return super().execute(sql, *args, **kwargs)
        def commit(self):
            if phase == "commit":
                raise error
            return super().commit()
        def rollback(self):
            super().rollback()
            raise sqlite3.OperationalError("secondary rollback")
        def close(self):
            super().close()
            raise sqlite3.OperationalError("secondary close")
    def connect(*args, **kwargs):
        if phase == "connect":
            raise error
        return original(*args, **kwargs, factory=Connection)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        with pytest.raises(sqlite3.OperationalError) as caught:
            with database.connect(write=True) as connection:
                calls.append(True)
                connection.execute("UPDATE probe SET value=99")
                if phase == "body":
                    raise error
    assert caught.value is error
    assert len(calls) == int(phase in {"body", "commit"})
    details = storage_error_details(error)
    assert details == {"database": "state.sqlite3", "phase": {"connect": "open", "body": "transaction"}.get(phase, phase),
                       "type": "OperationalError", "sqlite_errorcode": code, "sqlite_errorname": name}
    assert name in caplog.text and "File " in caplog.text
    assert "PRIVATE SQL PARAMETER" not in caplog.text and "secondary rollback" not in caplog.text
    with database.connect() as connection:
        assert connection.execute("SELECT value FROM probe").fetchone()[0] == 0


def _worker(root, count, start, results):
    from chatcopilot.harness.store import HarnessStore
    try:
        store = HarnessStore(Path(root))
        assert start.wait(15)
        for number in range(count):
            store.save_attempt(TASK, number + 1, {"number": number + 1, "status": "verifying"})
            store.update(TASK, heartbeat_at=number)
        results.put(None)
    except BaseException as error:
        results.put(repr(error))


def stress(root):
    from chatcopilot.harness.store import HarnessStore
    store = HarnessStore(root)
    store.create({"task_id": TASK, "request_key": "request", "request_digest": "digest",
                  "match_key": "match", "context_key": "context", "active_key": "active"})
    with store.database.connect(write=True) as connection:
        connection.executescript(SCHEMA)
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    worker = context.Process(target=_worker, args=(str(root), 200, start, results))
    def request(_):
        for _ in range(100):
            request_store = HarnessStore(root)
            request_store.get(TASK)
            with request_store.creation_guard(), request_store.database.connect(write=True) as connection:
                connection.execute("UPDATE probe SET value=value+1")
    try:
        worker.start()
        start.set()
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(request, range(6)))
        assert results.get(timeout=30) is None
        worker.join(10)
        assert worker.exitcode == 0
        assert store.get(TASK)["heartbeat_at"] == 199
        assert len(store.attempts(TASK)) == 200
        with store.database.connect() as connection:
            assert connection.execute("SELECT value FROM probe").fetchone()[0] == 600
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        print("600 threaded requests + 200 worker attempts/heartbeats; integrity_check=ok")
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("sandbox", [False, True], ids=["host-ext4", "bubblewrap-tmpfs"])
def test_threaded_console_and_worker_storage_stress(tmp_path, sandbox):
    if not sandbox:
        stress(tmp_path / "private")
        return
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap unavailable")
    result = subprocess.run(["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--tmpfs", "/tmp",
                             "--proc", "/proc", "--dev", "/dev", sys.executable, str(Path(__file__).resolve()),
                             "--stress", "/tmp/private"], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "integrity_check=ok" in result.stdout


if __name__ == "__main__":
    assert sys.argv[1] == "--stress"
    stress(Path(sys.argv[2]))
