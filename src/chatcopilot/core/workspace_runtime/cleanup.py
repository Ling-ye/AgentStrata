"""Workspace 自动清理 + 主动清空策略。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Tuple

from chatcopilot.core.workspace_runtime.model import Workspace

_LOGGER = logging.getLogger("chatcopilot.core.workspace_runtime.cleanup")

_DAY_SEC = 86400
_GB = 1024 ** 3
DIAGNOSTIC_RETENTION_DAYS = 30
DIAGNOSTIC_MAX_BYTES = 1 * _GB

# 老数据自动清理策略：(max_age_sec, max_total_bytes)
# attachments / downloads 是用户输入数据 → 严格；results 是 agent 产出报告 → 宽松。
CLEANUP_POLICIES: Dict[str, Dict[str, int]] = {
    "attachments": {"max_age_sec": 1 * _DAY_SEC, "max_total_bytes": 1 * _GB},
    "downloads":   {"max_age_sec": 1 * _DAY_SEC, "max_total_bytes": 1 * _GB},
    "results":     {"max_age_sec": 7 * _DAY_SEC, "max_total_bytes": 2 * _GB},
}

# 用户主动触发的"清空"覆盖的子目录（逻辑名）。
_CLEAR_TARGETS = ("downloads", "results", "uploads", "attachments")


def collect_files(target: Path) -> Tuple[List[Tuple[Path, int, float]], int]:
    """递归收集目录下所有文件 ``(path, size, mtime)``，返回 ``(entries, total_bytes)``。"""
    entries: List[Tuple[Path, int, float]] = []
    total = 0
    if not target.is_dir():
        return entries, 0
    for path in target.rglob("*"):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        entries.append((path, stat.st_size, stat.st_mtime))
        total += stat.st_size
    return entries, total


def _prune_empty_dirs(target: Path) -> None:
    """自下而上删空子目录（target 自身保留）。"""
    if not target.is_dir():
        return
    for sub in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not sub.is_dir():
            continue
        try:
            next(sub.iterdir())
        except StopIteration:
            try:
                sub.rmdir()
            except OSError:
                pass
        except OSError:
            pass


def _cleanup_one(target: Path, max_age_sec: int, max_total_bytes: int) -> Dict[str, int]:
    """对单个目录执行清理。"""
    deleted = 0
    freed = 0
    if not target.is_dir():
        return {"deleted_files": 0, "freed_bytes": 0}

    now = time.time()
    entries, total = collect_files(target)

    survivors: List[Tuple[Path, int, float]] = []
    for path, size, mtime in entries:
        if now - mtime > max_age_sec:
            try:
                path.unlink()
                deleted += 1
                freed += size
                total -= size
            except OSError:
                survivors.append((path, size, mtime))
        else:
            survivors.append((path, size, mtime))

    if total > max_total_bytes:
        survivors.sort(key=lambda x: x[2])
        for path, size, _mtime in survivors:
            if total <= max_total_bytes:
                break
            try:
                path.unlink()
                deleted += 1
                freed += size
                total -= size
            except OSError:
                continue

    _prune_empty_dirs(target)
    return {"deleted_files": deleted, "freed_bytes": freed}


def cleanup_workspace(ws: Workspace) -> Dict[str, Dict[str, int]]:
    """按 ``CLEANUP_POLICIES`` 静默清理 attachments / downloads / results。"""
    summary: Dict[str, Dict[str, int]] = {}
    for name, policy in CLEANUP_POLICIES.items():
        try:
            target = ws.resolve_subdir(name)
        except ValueError:
            continue
        try:
            stats = _cleanup_one(
                target,
                max_age_sec=int(policy["max_age_sec"]),
                max_total_bytes=int(policy["max_total_bytes"]),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("cleanup_workspace failed for %s", target)
            stats = {"deleted_files": 0, "freed_bytes": 0}
        summary[name] = stats
        if stats["deleted_files"]:
            _LOGGER.info(
                "cleanup_workspace: %s deleted=%d freed=%.2f MB",
                target,
                stats["deleted_files"],
                stats["freed_bytes"] / (1024 * 1024),
            )
    return summary


def clear_workspace_files(ws: Workspace) -> Dict[str, Dict[str, int]]:
    """清空 4 个产物子目录里的全部文件与嵌套子目录，保留子目录本身。

    不动 MEMORY.md / .cursor/rules / .cc-connect 其它内部状态文件。
    """
    summary: Dict[str, Dict[str, int]] = {}
    for name in _CLEAR_TARGETS:
        try:
            target = ws.resolve_subdir(name)
        except ValueError:
            continue

        if not target.is_dir():
            summary[name] = {"deleted_files": 0, "freed_bytes": 0}
            continue

        entries, total = collect_files(target)
        deleted_files = len(entries)
        freed = total

        try:
            shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("clear_workspace_files failed for %s", target)
            remaining, remaining_bytes = collect_files(target)
            deleted_files = max(0, deleted_files - len(remaining))
            freed = max(0, freed - remaining_bytes)

        summary[name] = {"deleted_files": deleted_files, "freed_bytes": freed}
        if deleted_files:
            _LOGGER.info(
                "clear_workspace_files: %s deleted=%d freed=%.2f MB",
                target,
                deleted_files,
                freed / (1024 * 1024),
            )
    return summary


def cleanup_diagnostic_records(
    workspace_root: Path,
    *,
    retention_days: int | None = None,
    max_total_bytes: int | None = None,
) -> Dict[str, int]:
    """Prune task/job/transcript evidence as cohesive records across an instance."""
    retention_days = retention_days if retention_days is not None else int(
        os.environ.get("CHATCOPILOT_DIAGNOSTIC_RETENTION_DAYS", DIAGNOSTIC_RETENTION_DAYS)
    )
    max_total_bytes = max_total_bytes if max_total_bytes is not None else int(
        os.environ.get("CHATCOPILOT_DIAGNOSTIC_MAX_BYTES", DIAGNOSTIC_MAX_BYTES)
    )
    if not workspace_root.is_dir():
        return {"deleted_records": 0, "freed_bytes": 0, "remaining_bytes": 0}

    records: list[tuple[Path, int, float, bool]] = []
    for pattern, status_name in (("**/tasks/task_*", "task.json"), ("**/jobs/job_*", "status.json")):
        for path in workspace_root.glob(pattern):
            if not path.is_dir():
                continue
            entries, size = collect_files(path)
            mtime = max((entry[2] for entry in entries), default=path.stat().st_mtime)
            status = ""
            try:
                payload = json.loads((path / status_name).read_text(encoding="utf-8"))
                status = str(payload.get("status") or "")
            except Exception:  # noqa: BLE001
                pass
            records.append((path, size, mtime, status in {"queued", "running"}))
    for path in workspace_root.glob("**/transcripts/*.jsonl"):
        if path.is_file():
            stat = path.stat()
            records.append((path, stat.st_size, stat.st_mtime, False))

    cutoff = time.time() - max(1, retention_days) * _DAY_SEC
    deleted = freed = 0

    def remove(path: Path, size: int) -> bool:
        nonlocal deleted, freed
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            return False
        deleted += 1
        freed += size
        return True

    survivors: list[tuple[Path, int, float, bool]] = []
    for record in records:
        path, size, mtime, active = record
        if not active and mtime < cutoff and remove(path, size):
            continue
        survivors.append(record)

    total = sum(size for path, size, _mtime, _active in survivors if path.exists())
    for path, size, _mtime, active in sorted(survivors, key=lambda item: item[2]):
        if total <= max_total_bytes:
            break
        if active or not path.exists():
            continue
        if remove(path, size):
            total -= size
    return {"deleted_records": deleted, "freed_bytes": freed, "remaining_bytes": max(0, total)}


__all__ = [
    "CLEANUP_POLICIES",
    "cleanup_workspace",
    "cleanup_diagnostic_records",
    "clear_workspace_files",
    "collect_files",
]
