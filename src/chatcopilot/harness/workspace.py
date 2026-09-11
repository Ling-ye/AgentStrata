"""Single-task Git worktrees and reversible product-only candidate changes."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text, private_directory
from chatcopilot.core.source_snapshot import git_output, manifest_digest, source_manifest
from chatcopilot.harness.models import HarnessError

_AREAS = (
    "agent",
    "application",
    "authorization",
    "botspec",
    "channels",
    "core",
    "external_tools",
    "gateway",
    "middleware",
    "platforms",
    "protocols",
    "tool_packs",
)
_FIXED_CORE = (
    "file_integrity.py",
    "private_sqlite.py",
    "source_snapshot.py",
    "source_manifest.py",
    "inspection.py",
)


def writable_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        root / "src" / "chatcopilot" / area
        for area in _AREAS
        if (root / "src" / "chatcopilot" / area).is_dir()
    )


def protected_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        root / "src" / "chatcopilot" / "core" / name
        for name in _FIXED_CORE
        if (root / "src" / "chatcopilot" / "core" / name).exists()
    )


def permitted_change(name: str) -> bool:
    parts = Path(name).parts
    return (
        len(parts) > 3
        and parts[:2] == ("src", "chatcopilot")
        and parts[2] in _AREAS
        and not (parts[2] == "core" and parts[-1] in _FIXED_CORE)
    )


def prepare(repository: Path, root: Path, task_id: str, commit: str) -> Path:
    if not re.fullmatch(r"repair-[0-9a-f]{32}", task_id):
        raise ValueError("invalid repair ID")
    directory = private_directory(root / "jobs" / task_id)
    path = directory / "worktree"
    branch = "feat/harness-" + task_id.removeprefix("repair-")
    if path.exists():
        if path.is_symlink() or git_output(path, "rev-parse", "HEAD") != commit:
            raise HarnessError("workspace_changed", "修复工作区基线已变化")
        return path
    git_output(repository, "worktree", "add", "-b", branch, str(path), commit)
    return path


def delta(worktree: Path, baseline: dict[str, Any]) -> list[str]:
    current = source_manifest(worktree)
    changed = sorted(
        name for name in baseline.keys() | current.keys() if baseline.get(name) != current.get(name)
    )
    if any(not permitted_change(name) for name in changed):
        raise HarnessError("protected_change", "候选修改了测试、评分或运行控制文件")
    return changed


def save_patch(worktree: Path, baseline: dict[str, Any], output: Path) -> str:
    changes = delta(worktree, baseline)
    command = ["git", "-C", str(worktree), "diff", "--binary", "HEAD", "--", *changes]
    result = subprocess.run(command, capture_output=True, timeout=30, check=True)
    content = result.stdout
    for name in changes:
        if name not in baseline:
            added = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "diff",
                    "--no-index",
                    "--binary",
                    "--",
                    "/dev/null",
                    name,
                ],
                capture_output=True,
                timeout=30,
            )
            if added.returncode not in (0, 1):
                raise HarnessError("patch_failed", "无法导出新增文件补丁")
            content += added.stdout
    fd = os.open(output, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
    return hashlib.sha256(content).hexdigest()


def restore(worktree: Path, baseline: dict[str, Any], expected_digest: str) -> None:
    if manifest_digest(source_manifest(worktree)) != expected_digest:
        raise HarnessError("workspace_changed", "候选工作区被外部修改，停止自动恢复")
    changed = delta(worktree, baseline)
    for name in changed:
        target = worktree / name
        if target.resolve().parent != target.parent.resolve() or target.is_symlink():
            raise HarnessError("workspace_changed", "候选路径不再安全")
        if name not in baseline:
            target.unlink()
        else:
            data = subprocess.run(
                ["git", "-C", str(worktree), "show", f"HEAD:{name}"],
                capture_output=True,
                timeout=30,
                check=True,
            ).stdout
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".harness-restore")
            with temporary.open("xb") as stream:
                stream.write(data)
            temporary.chmod(0o755 if baseline[name]["executable"] else 0o644)
            temporary.replace(target)
    if manifest_digest(source_manifest(worktree)) != manifest_digest(baseline):
        raise HarnessError("workspace_changed", "工作区恢复后与基线不一致")


def context_key(source: dict[str, Any]) -> str:
    if source.get("kind") == "robot_task":
        return hashlib.sha256(
            json_text(
                {key: source[key] for key in ("kind", "bot_id", "run_id", "revision")}
            ).encode()
        ).hexdigest()
    conditions = source["conditions"]
    value = {key: source[key] for key in ("bot_id", "suite_id", "case_id", "target_id")}
    value["conditions"] = {
        "case": conditions["cases"].get(source["case_id"]),
        "target": conditions["targets"].get(source["target_id"]),
        "grading": conditions["grading"],
        "environment": conditions.get("environment"),
    }
    return hashlib.sha256(json_text(value).encode()).hexdigest()
