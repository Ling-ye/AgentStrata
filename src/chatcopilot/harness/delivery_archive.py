"""Verified recovery archives and task-owned Git resource cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
import uuid
from typing import Any

from chatcopilot.core.private_sqlite import json_text, private_directory, private_file
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest, verify_copy
from chatcopilot.harness.github_delivery import git
from chatcopilot.harness.models import ACTIVE, HarnessError, PIPELINE_VERSION


def task_paths(store: Any, task: dict[str, Any]) -> tuple[Path, Path, Path, str]:
    import re
    ident = task["task_id"]
    if task.get("pipeline_version") != PIPELINE_VERSION or not re.fullmatch(r"repair-[0-9a-f]{32}", ident):
        raise HarnessError("source_archived", "旧任务只读保留")
    directory = store.root / "jobs" / ident
    worktree = directory / "worktree"
    branch = "feat/harness-" + ident[7:]
    if directory.resolve() != directory or (task.get("worktree") and task["worktree"] != str(worktree)) or task.get("branch", branch) != branch:
        raise HarnessError("workspace_changed", "任务工作区位置或分支身份已变化")
    return Path(task["repository"]), directory, worktree, branch


def verify_worktree(store: Any, task: dict[str, Any]) -> Path:
    repository, _, root, branch = task_paths(store, task)
    if root.resolve() != root or git(root, "rev-parse", "--show-toplevel").decode().strip() != str(root):
        raise HarnessError("workspace_changed", "任务工作区已变化")
    if (git(root, "branch", "--show-current").decode().strip() != branch
            or git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            != git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")):
        raise HarnessError("workspace_changed", "任务 Git 所有权已变化")
    expected_head = task.get("working_head") or (task.get("local_commit") or {}).get("sha") or task["base_commit"]
    allowed_heads = {expected_head, (task.get("publication_intent") or {}).get("sha"), task.get("delivery", {}).get("update_commit_sha")}
    if git(root, "rev-parse", "HEAD").decode().strip() not in allowed_heads:
        raise HarnessError("workspace_changed", "任务分支包含未登记提交")
    if task.get("working_digest") and manifest_digest(source_manifest(root)) != task["working_digest"]:
        raise HarnessError("workspace_changed", "任务源码包含未登记修改")
    return root


def verify_archive(store: Any, task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    _, directory, _, _ = task_paths(store, task)
    reference = task.get("archive") or {}
    relative = Path(reference.get("path", ""))
    if not reference or relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("recovery",):
        raise HarnessError("archive_missing", "没有已验证的任务归档")
    folder = directory / relative
    if folder.resolve() != folder:
        raise HarnessError("archive_changed", "归档路径已变化")
    metadata = folder / "manifest.json"
    private_file(metadata)
    raw = metadata.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise HarnessError("archive_changed", "归档清单摘要不一致")
    data = json.loads(raw)
    verify_copy(folder / "source", data["manifest"])
    bundle = folder / "recovery.bundle"
    private_file(bundle)
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != data["bundle_sha256"]:
        raise HarnessError("archive_changed", "恢复所需 Git 数据已变化")
    patch = folder / "candidate.patch"
    private_file(patch)
    if hashlib.sha256(patch.read_bytes()).hexdigest() != data["patch_sha256"]:
        raise HarnessError("archive_changed", "归档补丁已变化")
    return folder, data


def archive(store: Any, task_id: str) -> dict[str, Any]:
    task = store.get(task_id)
    repository, directory, root, branch = task_paths(store, task)
    if not root.exists():
        if task.get("archive"):
            verify_archive(store, task)
        return task
    verify_worktree(store, task)
    manifest = source_manifest(root)
    head = git(root, "rev-parse", "HEAD").decode().strip()
    # Never remove non-ignored user files that the source archive cannot represent.
    extra = git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    if any(os.fsdecode(p) not in manifest for p in extra if p):
        raise HarnessError("cleanup_unarchived_files", "存在无法纳入源码归档的额外文件，保留工作区")
    if task.get("archive"):
        try:
            _, previous = verify_archive(store, task)
            if previous["head"] == head and previous["manifest"] == manifest:
                return task
        except (HarnessError, OSError, ValueError):
            # A damaged authoritative archive must not be overwritten to hide corruption.
            raise HarnessError("archive_changed", "已有归档校验失败，保留工作区") from None
    folder = private_directory(directory / "recovery" / uuid.uuid4().hex)
    copy_sources(root, folder / "source", manifest)
    git(root, "bundle", "create", str(folder / "recovery.bundle"), "HEAD")
    (folder / "recovery.bundle").chmod(0o600)
    git(repository, "bundle", "verify", str(folder / "recovery.bundle"))
    from chatcopilot.harness.patches import save_patch
    before = task["baseline_manifest"]
    paths = sorted(name for name in before.keys() | manifest.keys() if before.get(name) != manifest.get(name))
    patch_sha = save_patch(root, directory / "source", paths, folder / "candidate.patch")
    data = {"patch_sha256": patch_sha, "head": head, "branch": branch, "base_sha": task["base_commit"], "manifest": manifest,
            "bundle_sha256": hashlib.sha256((folder / "recovery.bundle").read_bytes()).hexdigest()}
    raw = json_text(data).encode()
    (folder / "manifest.json").write_bytes(raw)
    (folder / "manifest.json").chmod(0o600)
    verify_copy(folder / "source", manifest)
    if source_manifest(root) != manifest or git(root, "rev-parse", "HEAD").decode().strip() != head:
        raise HarnessError("workspace_changed", "归档期间工作区变化，保留现场")
    task = store.update(task_id, archive={"path": folder.relative_to(directory).as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(), "digest": manifest_digest(manifest), "head": head},
        cleanup={**task.get("cleanup", {}), "local": "archived"})
    verify_archive(store, task)
    return task


def cleanup_local(store: Any, task_id: str) -> None:
    task = store.get(task_id)
    repository, _, root, branch = task_paths(store, task)
    if task["status"] in {*ACTIVE, "waiting_input"} or task.get("current_evaluation_id") or task.get("delivery_evaluation"):
        raise HarnessError("cleanup_active", "任务仍在执行，暂不清理")
    if not task.get("worktree"):
        if root.exists():
            raise HarnessError("workspace_unowned", "工作区创建回执缺失，保留现场")
        store.update(task_id, cleanup={**task.get("cleanup", {}), "local": "not_created"})
        return
    if task.get("dispatch_state") == "scheduled":
        expected_unit = "agentstrata-harness-" + task_id[7:]
        if task.get("unit") != expected_unit:
            raise HarnessError("cleanup_process_unknown", "任务进程身份不匹配，保留工作区")
        probe = subprocess.run(["systemctl", "--user", "show", expected_unit,
                                "--property=LoadState", "--property=ActiveState"],
                               capture_output=True, text=True, timeout=10)
        fields = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
        if probe.returncode or fields.get("ActiveState") not in {"inactive", "failed"}:
            code = "cleanup_active" if fields.get("ActiveState") in {"active", "activating", "deactivating", "reloading"} else "cleanup_process_unknown"
            raise HarnessError(code, "等待任务进程退出并确认身份后清理")
    if {task.get("error_code"), task.get("delivery", {}).get("error_code")} & {
        "workspace_changed", "checkpoint_restore_failed", "index_changed"}:

        raise HarnessError("workspace_changed", "现场身份异常，保留工作区")
    task = archive(store, task_id)
    if task.get("archive"):
        _, data = verify_archive(store, task)
        expected = data["head"]
    elif not root.exists():
        expected = task.get("working_head") or task["base_commit"]
    else:
        raise HarnessError("archive_missing", "归档未完成")
    if root.exists():
        verify_worktree(store, task)
        # Dirty bytes are already preserved and verified. Only this task's registered worktree is removed.
        git(repository, "worktree", "remove", "--force", str(root))
    references = git(repository, "for-each-ref", "--format=%(objectname)", "refs/heads/" + branch).decode().splitlines()
    if references:
        if references != [expected]:
            raise HarnessError("workspace_changed", "任务本地分支已变化，停止删除")
        worktrees = git(repository, "worktree", "list", "--porcelain").decode()
        if "branch refs/heads/" + branch + "\n" in worktrees:
            raise HarnessError("cleanup_active", "任务分支仍被其他工作区使用")
        git(repository, "update-ref", "-d", "refs/heads/" + branch, expected)
    store.update(task_id, cleanup={**task.get("cleanup", {}), "local": "cleaned", "error": None})


def restore(store: Any, task_id: str) -> Path:
    task = store.get(task_id)
    repository, _, root, branch = task_paths(store, task)
    if root.exists():
        return verify_worktree(store, task)
    folder, data = verify_archive(store, task)
    git(repository, "fetch", "--no-write-fetch-head", str(folder / "recovery.bundle"), "HEAD")
    existing = git(repository, "for-each-ref", "--format=%(objectname)", "refs/heads/" + branch).decode().splitlines()
    if existing and existing != [data["head"]]:
        raise HarnessError("workspace_changed", "待恢复的分支名称已被其他提交占用")
    if existing:
        git(repository, "worktree", "add", str(root), branch)
    else:
        git(repository, "worktree", "add", "-b", branch, str(root), data["head"])
    from chatcopilot.harness.verification_policy import source_files
    for name in source_files(root):
        if name not in data["manifest"]:
            (root / name).unlink()
    for name, record in data["manifest"].items():
        path = root / name
        if path.resolve() != path:
            raise HarnessError("workspace_changed", "恢复目标不是普通文件路径")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((folder / "source" / name).read_bytes())
        path.chmod(0o700 if record["executable"] else 0o600)
    verify_copy(root, data["manifest"])
    store.update(task_id, working_head=data["head"], working_digest=manifest_digest(data["manifest"]),
                 cleanup={**task.get("cleanup", {}), "local": "restored"})
    return root
