"""Verified private history exports before replacing the active state database."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from chatcopilot.core.private_sqlite import json_text, private_directory, private_file
from chatcopilot.harness.models import HarnessError


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_history(store, task_ids):
    destination = private_directory(
        private_directory(store.root / "history-exports") / ("export-" + uuid.uuid4().hex)
    )
    files, omitted = {}, []

    def copy(source, relative):
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise HarnessError("cutover_path_unsafe", "历史产物存在符号链接，导出未完成")
        target = destination / relative
        if stat.S_ISDIR(info.st_mode):
            private_directory(target)
            for child in sorted(source.iterdir()):
                if (
                    (
                        relative.parts
                        and relative.parts[0] == "jobs"
                        and len(relative.parts) == 2
                        and child.name in {"runtime", "environment", "sessions"}
                    )
                    or child.name in {"auth.json", "worker.lock"}
                    or child.name.endswith(("-wal", "-shm", "-journal"))
                ):
                    omitted.append((relative / child.name).as_posix())
                    continue
                copy(child, relative / child.name)
            _sync_directory(target)
            return
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise HarnessError("cutover_path_unsafe", "历史产物类型、所有权或链接数不安全")
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        if source.suffix == ".sqlite3":
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as connection:
                with sqlite3.connect(target) as backup:
                    connection.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise HarnessError("history_export_invalid", "历史数据库备份校验失败")
        else:
            with source.open("rb") as incoming, target.open("wb") as outgoing:
                for data in iter(lambda: incoming.read(1024 * 1024), b""):
                    outgoing.write(data)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if _digest(source) != _digest(target):
                raise HarnessError("history_export_invalid", "导出期间历史产物发生变化")
        files[relative.as_posix()] = {"sha256": _digest(target), "mode": stat.S_IMODE(info.st_mode)}

    copy(store.database.path, Path("harness.sqlite3"))
    for name in ("jobs", "archives", "graphs"):
        root = store.root / name
        if root.exists():
            copy(root, Path(name))
    path = destination / "manifest.json"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(
            json_text(
                {
                    "task_ids": sorted(task_ids),
                    "files": files,
                    "omitted_execution_material": omitted,
                }
            )
        )
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(destination)
    _sync_directory(destination.parent)
    verify_history(destination)
    return {"path": str(destination), "manifest_sha256": _digest(path), "files": len(files)}


def verify_history(directory):
    directory = private_directory(directory)
    path = directory / "manifest.json"
    private_file(path)
    value = json.loads(path.read_text())
    for relative, record in value["files"].items():
        target = directory / relative
        if target.resolve() != target or not target.is_relative_to(directory):
            raise HarnessError("history_export_invalid", "历史导出路径不安全")
        private_file(target)
        if _digest(target) != record["sha256"]:
            raise HarnessError("history_export_invalid", "历史导出内容与清单不符")
    return value
