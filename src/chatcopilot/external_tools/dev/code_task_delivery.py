"""Trusted GitHub pull-request delivery for isolated code tasks."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence

import requests

from chatcopilot.contracts.code_tasks import validate_code_task_title
from chatcopilot.contracts.tools import ToolHandlerError
from chatcopilot.core.jobs import read_json_file, write_json_atomic
from chatcopilot.external_tools.dev.config import get_dev_config
from chatcopilot.external_tools.dev.path_guard import (
    DevPathAccessError,
    ensure_writable,
)
from chatcopilot.project import ENV_PREFIX

DELIVERY_FILENAME = "delivery.json"
VALIDATION_INDEX_FILENAME = ".validation-index"

_REPOSITORY_ENV = f"{ENV_PREFIX}_CODE_TASK_GITHUB_REPOSITORY"
_TOKEN_FILE_ENV = f"{ENV_PREFIX}_CODE_TASK_GITHUB_TOKEN_FILE"
_AUTHOR_NAME_ENV = f"{ENV_PREFIX}_CODE_TASK_GIT_AUTHOR_NAME"
_AUTHOR_EMAIL_ENV = f"{ENV_PREFIX}_CODE_TASK_GIT_AUTHOR_EMAIL"
_REPOSITORY_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9_.-]+)"
)
_SAFE_REF_PART_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_GITHUB_API_ROOT = "https://api.github.com"
_REMOTE_TIMEOUT_SECONDS = 120
_API_TIMEOUT_SECONDS = 30
_DELIVERY_STATE_KEYS = frozenset(
    {
        "repository",
        "base_branch",
        "base_sha",
        "branch",
        "commit_sha",
        "tree_sha",
        "pr_number",
        "pr_url",
        "draft",
        "prepared_at",
        "committed_at",
        "pushed_at",
        "delivered_at",
        "updated_at",
    }
)


@dataclass(frozen=True)
class GitHubDeliveryConfig:
    repository: str
    owner: str
    token_file: Path
    token: str = field(repr=False)
    author_name: str
    author_email: str


def load_delivery_config() -> GitHubDeliveryConfig:
    raw_repository = os.environ.get(_REPOSITORY_ENV, "").strip().removesuffix(".git")
    match = _REPOSITORY_RE.fullmatch(raw_repository)
    if match is None:
        raise ToolHandlerError(
            f"{_REPOSITORY_ENV} must be owner/repository",
            error_code="code_task_github_repository_invalid",
            stage="preparing",
        )
    token_file, token = _load_token_file(
        os.environ.get(_TOKEN_FILE_ENV, "").strip()
    )
    author_name = os.environ.get(_AUTHOR_NAME_ENV, "").strip()
    author_email = os.environ.get(_AUTHOR_EMAIL_ENV, "").strip()
    if not author_name or "\n" in author_name or _CONTROL_RE.search(author_name):
        raise ToolHandlerError(
            f"{_AUTHOR_NAME_ENV} is required",
            error_code="code_task_git_author_invalid",
            stage="preparing",
        )
    if (
        not author_email
        or "\n" in author_email
        or _CONTROL_RE.search(author_email)
        or not re.fullmatch(r"[^@\s]+@[^@\s]+", author_email)
    ):
        raise ToolHandlerError(
            f"{_AUTHOR_EMAIL_ENV} must be a valid email address",
            error_code="code_task_git_author_invalid",
            stage="preparing",
        )
    return GitHubDeliveryConfig(
        repository=raw_repository,
        owner=match.group("owner"),
        token_file=token_file,
        token=token,
        author_name=author_name,
        author_email=author_email,
    )


def prepare_delivery_worktree(
    *,
    job_dir: Path,
    worktree: Path,
    instance_id: str,
    source_root: Path,
) -> dict[str, Any]:
    """Clone the GitHub default branch into a job-private repository."""
    config = load_delivery_config()
    _verify_source_origin(
        source_root,
        config,
        job_dir=job_dir,
    )
    state = _delivery_state(job_dir)
    if state:
        if not worktree.is_dir():
            raise ToolHandlerError(
                "retained code-task clone is missing",
                error_code="code_task_worktree_missing",
                stage="preparing",
            )
        _verify_state_target(state, config)
        return state
    if worktree.exists():
        if (
            worktree.parent.resolve() != job_dir.resolve()
            or worktree.name != "worktree"
        ):
            raise ToolHandlerError(
                "unexpected code-task worktree path",
                error_code="code_task_worktree_exists",
                stage="preparing",
            )
        if worktree.is_symlink() or not worktree.is_dir():
            worktree.unlink()
        else:
            shutil.rmtree(worktree)

    metadata = _github_request(
        config,
        "GET",
        f"/repos/{config.repository}",
        stage="preparing",
    )
    base_branch = str(metadata.get("default_branch") or "").strip()
    if not _valid_ref_component(base_branch):
        raise ToolHandlerError(
            "GitHub repository has no valid default branch",
            error_code="code_task_github_default_branch_invalid",
            stage="preparing",
        )
    branch = _code_task_branch(instance_id, job_dir.name)
    remote_url = _github_git_url(config)
    try:
        with _remote_git_environment(config, job_dir) as env:
            _run_git(
                [
                    "clone",
                    "--depth",
                    "1",
                    "--no-tags",
                    "--single-branch",
                    "--branch",
                    base_branch,
                    "--origin",
                    "origin",
                    remote_url,
                    str(worktree),
                ],
                cwd=job_dir,
                env=env,
                config=config,
                stage="preparing",
            )
    except Exception:
        if worktree.parent.resolve() == job_dir.resolve():
            shutil.rmtree(worktree, ignore_errors=True)
        raise
    _run_git(
        ["switch", "-c", branch],
        cwd=worktree,
        env=_local_git_environment(job_dir),
        stage="preparing",
    )
    base_sha = _git_output(
        ["rev-parse", "HEAD"],
        cwd=worktree,
        env=_local_git_environment(job_dir),
        stage="preparing",
    )
    now = time.time()
    state = {
        "repository": config.repository,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "branch": branch,
        "commit_sha": "",
        "tree_sha": "",
        "pr_number": None,
        "pr_url": "",
        "draft": None,
        "prepared_at": now,
        "updated_at": now,
    }
    _write_delivery_state(job_dir, state)
    return state


def delivery_retry_pending(job_dir: Path) -> bool:
    state = _delivery_state(job_dir)
    if not state:
        return False
    changes = read_json_file(job_dir / "changes.json") or {}
    validation = read_json_file(job_dir / "validation.json") or {}
    files = changes.get("files")
    changed_files = [
        str(item.get("path"))
        for item in (files if isinstance(files, list) else [])
        if isinstance(item, Mapping) and item.get("path")
    ]
    validated_tree_sha = str(
        validation.get("validated_tree_sha") or ""
    )
    if (
        not changed_files
        or validation.get("status") != "passed"
        or not validated_tree_sha
    ):
        return False
    recorded_tree_sha = str(state.get("tree_sha") or "")
    if recorded_tree_sha:
        if recorded_tree_sha != validated_tree_sha:
            raise ToolHandlerError(
                "delivery tree differs from validation evidence",
                error_code="code_task_delivery_tree_mismatch",
                stage="preparing",
            )
        return bool(state.get("commit_sha"))

    worktree = job_dir / "worktree"
    if not worktree.is_dir():
        raise ToolHandlerError(
            "validated delivery clone is missing",
            error_code="code_task_worktree_missing",
            stage="preparing",
        )
    head_sha = _git_output(
        ["rev-parse", "HEAD"],
        cwd=worktree,
        env=_local_git_environment(job_dir),
        stage="preparing",
    )
    base_sha = str(state.get("base_sha") or "")
    if head_sha != base_sha:
        status = _git_output(
            ["status", "--porcelain=v1", "-z"],
            cwd=worktree,
            env=_local_git_environment(job_dir),
            stage="preparing",
        )
        head_tree = _git_output(
            ["rev-parse", "HEAD^{tree}"],
            cwd=worktree,
            env=_local_git_environment(job_dir),
            stage="preparing",
        )
        if status or head_tree != validated_tree_sha:
            raise ToolHandlerError(
                "local delivery commit differs from validation evidence",
                error_code="code_task_delivery_tree_mismatch",
                stage="preparing",
            )
        return True
    current_tree = compute_delivery_tree(
        job_dir=job_dir,
        worktree=worktree,
        changed_files=changed_files,
        stage="preparing",
    )
    if current_tree != validated_tree_sha:
        raise ToolHandlerError(
            "working tree differs from validation evidence",
            error_code="code_task_delivery_tree_mismatch",
            stage="preparing",
        )
    return True


def deliver_pull_request(
    *,
    job_dir: Path,
    worktree: Path,
    title: str,
    changed_files: Sequence[str],
    checks: Sequence[str],
    validated_tree_sha: str,
) -> dict[str, Any]:
    """Commit validated changes, push a unique branch, and open a draft PR."""
    public_title = validate_code_task_title(title)
    config = load_delivery_config()
    state = _delivery_state(job_dir)
    if not state:
        raise ToolHandlerError(
            "delivery state is missing",
            error_code="code_task_delivery_state_missing",
            stage="delivering",
        )
    _verify_state_target(state, config)
    paths = tuple(dict.fromkeys(str(path) for path in changed_files if str(path)))
    if not paths:
        return {**state, "delivered": False}
    expected_tree = str(validated_tree_sha or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", expected_tree):
        raise ToolHandlerError(
            "validated Git tree evidence is missing",
            error_code="code_task_validation_evidence_missing",
            stage="delivering",
        )
    recorded_tree = str(state.get("tree_sha") or "")
    if recorded_tree and recorded_tree != expected_tree:
        raise ToolHandlerError(
            "delivery tree differs from validation evidence",
            error_code="code_task_delivery_tree_mismatch",
            stage="delivering",
        )
    if state.get("pr_url"):
        if (
            not state.get("commit_sha")
            or recorded_tree != expected_tree
            or state.get("draft") is not True
        ):
            raise ToolHandlerError(
                "completed delivery state is inconsistent",
                error_code="code_task_delivery_state_invalid",
                stage="delivering",
            )
        return {**state, "delivered": True}

    validate_delivery_paths(paths, stage="delivering")
    local_env = _local_git_environment(
        job_dir,
        author_name=config.author_name,
        author_email=config.author_email,
    )
    commit_sha = str(state.get("commit_sha") or "")
    if not commit_sha:
        if not worktree.is_dir():
            raise ToolHandlerError(
                "code-task clone is missing before commit",
                error_code="code_task_worktree_missing",
                stage="delivering",
            )
        base_sha = str(state.get("base_sha") or "")
        head_sha = _git_output(
            ["rev-parse", "HEAD"],
            cwd=worktree,
            env=local_env,
            stage="delivering",
        )
        if head_sha != base_sha:
            commit_sha = _recover_unrecorded_commit(
                worktree=worktree,
                env=local_env,
                base_sha=base_sha,
                expected_paths=paths,
                expected_message=public_title,
                expected_tree_sha=expected_tree,
            )
        else:
            current_tree = compute_delivery_tree(
                job_dir=job_dir,
                worktree=worktree,
                changed_files=paths,
                stage="delivering",
            )
            if current_tree != expected_tree:
                raise ToolHandlerError(
                    "working tree differs from validation evidence",
                    error_code="code_task_delivery_tree_mismatch",
                    stage="delivering",
                )
            _run_git(
                ["add", "--all", "--", *paths],
                cwd=worktree,
                env=local_env,
                stage="delivering",
            )
            staged = _git_output(
                ["diff", "--cached", "--name-only", "-z"],
                cwd=worktree,
                env=local_env,
                stage="delivering",
            )
            staged_paths = tuple(item for item in staged.split("\0") if item)
            if set(staged_paths) != set(paths):
                raise ToolHandlerError(
                    "staged files differ from the validated task delta",
                    error_code="code_task_delivery_delta_mismatch",
                    stage="delivering",
                    details={
                        "expected": sorted(paths),
                        "staged": sorted(staged_paths),
                    },
                )
            staged_tree = _git_output(
                ["write-tree"],
                cwd=worktree,
                env=local_env,
                stage="delivering",
            )
            if staged_tree != expected_tree:
                raise ToolHandlerError(
                    "staged Git tree differs from validation evidence",
                    error_code="code_task_delivery_tree_mismatch",
                    stage="delivering",
                )
            _run_git(
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "--no-verify",
                    "--no-gpg-sign",
                    "-m",
                    public_title,
                ],
                cwd=worktree,
                env=local_env,
                stage="delivering",
            )
            commit_sha = _git_output(
                ["rev-parse", "HEAD"],
                cwd=worktree,
                env=local_env,
                stage="delivering",
            )
            commit_tree = _git_output(
                ["rev-parse", "HEAD^{tree}"],
                cwd=worktree,
                env=local_env,
                stage="delivering",
            )
            if commit_tree != expected_tree:
                raise ToolHandlerError(
                    "committed Git tree differs from validation evidence",
                    error_code="code_task_delivery_tree_mismatch",
                    stage="delivering",
                )
        state = {
            **state,
            "commit_sha": commit_sha,
            "tree_sha": expected_tree,
            "committed_at": time.time(),
        }
        _write_delivery_state(job_dir, state)

    state = _ensure_remote_branch(
        config=config,
        job_dir=job_dir,
        worktree=worktree,
        state=state,
    )
    state = _ensure_draft_pr(
        config=config,
        job_dir=job_dir,
        state=state,
        title=public_title,
        changed_files=paths,
        checks=checks,
    )
    return {**state, "delivered": True}


def validate_delivery_paths(
    paths: Sequence[str],
    *,
    stage: str,
) -> None:
    config = get_dev_config(force_reload=True)
    violations: list[str] = []
    for rel in paths:
        try:
            ensure_writable(config, rel)
        except DevPathAccessError:
            violations.append(rel)
    if violations:
        raise ToolHandlerError(
            "code-task changes exceed context.dev write scope",
            error_code="code_task_scope_violation",
            stage=stage,
            details={"violating_files": sorted(violations)},
        )


def compute_delivery_tree(
    *,
    job_dir: Path,
    worktree: Path,
    changed_files: Sequence[str],
    stage: str,
) -> str:
    index = job_dir / ".delivery-index"
    try:
        return _populate_candidate_index(
            index=index,
            job_dir=job_dir,
            worktree=worktree,
            changed_files=changed_files,
            stage=stage,
        )
    finally:
        _cleanup_candidate_index(index, stage=stage)


def prepare_validation_index(
    *,
    job_dir: Path,
    worktree: Path,
    changed_files: Sequence[str],
    stage: str = "validating",
) -> tuple[Path, str]:
    """Build and retain the exact candidate index used by repository checks."""
    index = job_dir / VALIDATION_INDEX_FILENAME
    try:
        tree_sha = _populate_candidate_index(
            index=index,
            job_dir=job_dir,
            worktree=worktree,
            changed_files=changed_files,
            stage=stage,
        )
        index.chmod(0o600)
        info = index.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ToolHandlerError(
                "validation Git index must be a private regular file",
                error_code="code_task_validation_index_invalid",
                stage=stage,
            )
        return index, tree_sha
    except Exception:
        _cleanup_candidate_index(index, stage=stage)
        raise


def cleanup_validation_index(job_dir: Path) -> None:
    """Remove the retained validation index and any abandoned lock file."""
    _cleanup_candidate_index(
        job_dir / VALIDATION_INDEX_FILENAME,
        stage="validating",
    )


def _populate_candidate_index(
    *,
    index: Path,
    job_dir: Path,
    worktree: Path,
    changed_files: Sequence[str],
    stage: str,
) -> str:
    paths = tuple(
        dict.fromkeys(str(path) for path in changed_files if str(path))
    )
    if not paths:
        raise ToolHandlerError(
            "changed files are required for delivery tree evidence",
            error_code="code_task_validation_evidence_missing",
            stage=stage,
        )
    if index.parent.resolve() != job_dir.resolve():
        raise ToolHandlerError(
            "candidate Git index escaped the code-task job directory",
            error_code="code_task_validation_index_invalid",
            stage=stage,
        )
    _cleanup_candidate_index(index, stage=stage)
    env = _local_git_environment(job_dir)
    env["GIT_INDEX_FILE"] = str(index)
    _run_git(
        ["read-tree", "HEAD"],
        cwd=worktree,
        env=env,
        stage=stage,
    )
    _run_git(
        ["add", "--all", "--", "."],
        cwd=worktree,
        env=env,
        stage=stage,
    )
    staged = _git_output(
        ["diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--"],
        cwd=worktree,
        env=env,
        stage=stage,
    )
    staged_paths = {item for item in staged.split("\0") if item}
    if staged_paths != set(paths):
        raise ToolHandlerError(
            "working tree paths differ from validation evidence",
            error_code="code_task_delivery_delta_mismatch",
            stage=stage,
        )
    return _git_output(
        ["write-tree"],
        cwd=worktree,
        env=env,
        stage=stage,
    )


def _cleanup_candidate_index(index: Path, *, stage: str) -> None:
    failed: list[str] = []
    for label, candidate in (
        ("index", index),
        ("lock", index.with_name(f"{index.name}.lock")),
    ):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            failed.append(label)
            continue
        if stat.S_ISDIR(info.st_mode):
            failed.append(label)
            continue
        try:
            candidate.unlink()
        except OSError:
            failed.append(label)
            continue
        if candidate.exists() or candidate.is_symlink():
            failed.append(label)
    if failed:
        raise ToolHandlerError(
            "candidate Git index artifacts could not be removed",
            error_code="code_task_validation_cleanup_failed",
            stage=stage,
            details={"artifacts": sorted(set(failed))},
        )


def _recover_unrecorded_commit(
    *,
    worktree: Path,
    env: Mapping[str, str],
    base_sha: str,
    expected_paths: Sequence[str],
    expected_message: str,
    expected_tree_sha: str,
) -> str:
    if not base_sha:
        raise ToolHandlerError(
            "delivery base commit is missing",
            error_code="code_task_delivery_state_invalid",
            stage="delivering",
        )
    status = _git_output(
        ["status", "--porcelain=v1", "-z"],
        cwd=worktree,
        env=env,
        stage="delivering",
    )
    count = _git_output(
        ["rev-list", "--count", f"{base_sha}..HEAD"],
        cwd=worktree,
        env=env,
        stage="delivering",
    )
    delta = _git_output(
        ["diff", "--name-only", "--no-renames", "-z", f"{base_sha}..HEAD", "--"],
        cwd=worktree,
        env=env,
        stage="delivering",
    )
    subject = _git_output(
        ["log", "-1", "--format=%s"],
        cwd=worktree,
        env=env,
        stage="delivering",
    )
    head_tree = _git_output(
        ["rev-parse", "HEAD^{tree}"],
        cwd=worktree,
        env=env,
        stage="delivering",
    )
    delta_paths = {item for item in delta.split("\0") if item}
    if (
        status
        or count != "1"
        or delta_paths != set(expected_paths)
        or subject != expected_message
        or head_tree != expected_tree_sha
    ):
        raise ToolHandlerError(
            "unrecorded local commit does not match validated delivery evidence",
            error_code="code_task_delivery_delta_mismatch",
            stage="delivering",
        )
    return _git_output(
        ["rev-parse", "HEAD"],
        cwd=worktree,
        env=env,
        stage="delivering",
    )


def _ensure_remote_branch(
    *,
    config: GitHubDeliveryConfig,
    job_dir: Path,
    worktree: Path,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    branch = str(state.get("branch") or "")
    commit_sha = str(state.get("commit_sha") or "")
    remote_url = _github_git_url(config)
    with _remote_git_environment(config, job_dir) as env:
        remote_line = _git_output(
            ["ls-remote", "--heads", remote_url, f"refs/heads/{branch}"],
            cwd=job_dir,
            env=env,
            config=config,
            stage="delivering",
        )
        remote_sha = remote_line.split(None, 1)[0] if remote_line else ""
        if remote_sha and remote_sha != commit_sha:
            raise ToolHandlerError(
                "remote code-task branch points to a different commit",
                error_code="code_task_remote_branch_conflict",
                stage="delivering",
                details={"branch": branch},
            )
        if not remote_sha:
            if not worktree.is_dir():
                raise ToolHandlerError(
                    "local commit is unavailable for push",
                    error_code="code_task_delivery_commit_missing",
                    stage="delivering",
                )
            local_sha = _git_output(
                ["rev-parse", "HEAD"],
                cwd=worktree,
                env=_local_git_environment(job_dir),
                stage="delivering",
            )
            if local_sha != commit_sha:
                raise ToolHandlerError(
                    "local code-task commit differs from delivery evidence",
                    error_code="code_task_delivery_commit_mismatch",
                    stage="delivering",
                )
            _run_git(
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "push",
                    "--porcelain",
                    remote_url,
                    f"HEAD:refs/heads/{branch}",
                ],
                cwd=worktree,
                env=env,
                config=config,
                stage="delivering",
            )
    updated = {
        **state,
        "pushed_at": time.time(),
    }
    _write_delivery_state(job_dir, updated)
    return updated


def _ensure_draft_pr(
    *,
    config: GitHubDeliveryConfig,
    job_dir: Path,
    state: Mapping[str, Any],
    title: str,
    changed_files: Sequence[str],
    checks: Sequence[str],
) -> dict[str, Any]:
    branch = str(state.get("branch") or "")
    base_branch = str(state.get("base_branch") or "")
    commit_sha = str(state.get("commit_sha") or "")
    existing = _github_request(
        config,
        "GET",
        f"/repos/{config.repository}/pulls",
        params={
            "state": "open",
            "head": f"{config.owner}:{branch}",
            "base": base_branch,
        },
    )
    if not isinstance(existing, list):
        raise ToolHandlerError(
            "GitHub pull-request lookup returned an invalid response",
            error_code="code_task_github_response_invalid",
            stage="delivering",
        )
    pr: Mapping[str, Any] | None = None
    if existing:
        candidate = existing[0]
        if not isinstance(candidate, Mapping):
            raise ToolHandlerError(
                "GitHub pull-request lookup returned an invalid item",
                error_code="code_task_github_response_invalid",
                stage="delivering",
            )
        if candidate.get("state") != "open" or candidate.get("draft") is not True:
            raise ToolHandlerError(
                "existing pull request is not an open draft",
                error_code="code_task_pull_request_not_draft",
                stage="delivering",
            )
        pr = candidate
    if pr is None:
        created = _github_request(
            config,
            "POST",
            f"/repos/{config.repository}/pulls",
            json_body={
                "title": title,
                "head": branch,
                "base": base_branch,
                "body": _pull_request_body(
                    changed_files=changed_files,
                    checks=checks,
                ),
                "draft": True,
                "maintainer_can_modify": True,
            },
        )
        if not isinstance(created, Mapping):
            raise ToolHandlerError(
                "GitHub pull-request creation returned an invalid response",
                error_code="code_task_github_response_invalid",
                stage="delivering",
            )
        pr = created
    head = pr.get("head")
    head_sha = str(head.get("sha") or "") if isinstance(head, Mapping) else ""
    if head_sha != commit_sha:
        raise ToolHandlerError(
            "pull request does not point to the validated commit",
            error_code="code_task_pull_request_conflict",
            stage="delivering",
        )
    pr_url = str(pr.get("html_url") or "")
    pr_number = pr.get("number")
    draft = pr.get("draft")
    pr_state = pr.get("state")
    if (
        not pr_url
        or not isinstance(pr_number, int)
        or draft is not True
        or pr_state != "open"
    ):
        raise ToolHandlerError(
            "GitHub pull-request response is not an open draft",
            error_code="code_task_github_response_invalid",
            stage="delivering",
        )
    updated = {
        **state,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "draft": draft,
        "delivered_at": time.time(),
    }
    _write_delivery_state(job_dir, updated)
    return updated


def _pull_request_body(
    *,
    changed_files: Sequence[str],
    checks: Sequence[str],
) -> str:
    known_checks = tuple(
        name for name in ("quick", "full") if name in set(checks)
    )
    verification = "\n".join(f"- `{name}`: passed" for name in known_checks)
    if not verification:
        verification = "- Passed checks: 0"
    else:
        verification += f"\n- Passed checks: {len(known_checks)}"
    return (
        "## Problem\n\n"
        "Automated change prepared in an isolated code-task clone.\n\n"
        "## Changes\n\n"
        f"- Changed repository files: {len(set(changed_files))}\n"
        "- Review the pull-request diff for details.\n\n"
        "## Verification\n\n"
        f"{verification}\n\n"
        "## Release notes\n\n"
        "Draft PR created by the code-task worker; merge and deployment require human review.\n"
    )

def _github_request(
    config: GitHubDeliveryConfig,
    method: str,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
    stage: str = "delivering",
) -> Any:
    token = config.token
    try:
        response = requests.request(
            method,
            f"{_GITHUB_API_ROOT}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "AgentStrata-code-task-worker",
            },
            params=dict(params or {}),
            json=dict(json_body) if json_body is not None else None,
            timeout=_API_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ToolHandlerError(
            f"GitHub request failed: {type(exc).__name__}",
            error_code="code_task_github_unavailable",
            stage=stage,
        ) from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise ToolHandlerError(
            f"GitHub request failed with HTTP {response.status_code}",
            error_code="code_task_github_request_failed",
            stage=stage,
            details={"status_code": response.status_code},
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ToolHandlerError(
            "GitHub returned invalid JSON",
            error_code="code_task_github_response_invalid",
            stage=stage,
        ) from exc


def _delivery_state(job_dir: Path) -> dict[str, Any]:
    path = job_dir / DELIVERY_FILENAME
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ToolHandlerError(
            "delivery state must be a regular non-symlink file",
            error_code="code_task_delivery_state_invalid",
            stage="delivering",
        )
    payload = read_json_file(path)
    if payload is None:
        raise ToolHandlerError(
            "delivery state is unreadable or invalid",
            error_code="code_task_delivery_state_invalid",
            stage="delivering",
        )
    unknown = set(payload) - _DELIVERY_STATE_KEYS
    if unknown:
        raise ToolHandlerError(
            "delivery state contains unsupported fields",
            error_code="code_task_delivery_state_invalid",
            stage="delivering",
        )
    return payload


def _write_delivery_state(job_dir: Path, payload: Mapping[str, Any]) -> None:
    state = {
        key: value
        for key, value in payload.items()
        if key in _DELIVERY_STATE_KEYS
    }
    state["updated_at"] = time.time()
    write_json_atomic(job_dir / DELIVERY_FILENAME, state)


def _verify_state_target(
    state: Mapping[str, Any],
    config: GitHubDeliveryConfig,
) -> None:
    if str(state.get("repository") or "") != config.repository:
        raise ToolHandlerError(
            "delivery repository changed after task preparation",
            error_code="code_task_delivery_target_drift",
            stage="delivering",
        )



def _verify_source_origin(
    source_root: Path,
    config: GitHubDeliveryConfig,
    *,
    job_dir: Path,
) -> None:
    try:
        remote = _git_output(
            ["remote", "get-url", "origin"],
            cwd=source_root,
            env=_local_git_environment(job_dir),
            stage="preparing",
        )
    except ToolHandlerError as exc:
        raise ToolHandlerError(
            "source repository must have an origin remote",
            error_code="code_task_source_origin_missing",
            stage="preparing",
        ) from exc
    actual = _github_repository_from_remote(remote)
    if actual is None or actual.casefold() != config.repository.casefold():
        raise ToolHandlerError(
            "source origin does not match configured GitHub repository",
            error_code="code_task_source_origin_mismatch",
            stage="preparing",
        )


def _github_repository_from_remote(remote: str) -> str | None:
    value = remote.strip().removesuffix("/")
    match = re.fullmatch(
        r"(?:https?://|ssh://git@)github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        match = re.fullmatch(
            r"git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?",
            value,
            flags=re.IGNORECASE,
        )
    if match is None:
        return None
    repository = match.group("repo").removesuffix(".git")
    return repository if _REPOSITORY_RE.fullmatch(repository) else None

def _code_task_branch(instance_id: str, job_id: str) -> str:
    instance = _safe_ref_part(instance_id or "instance")
    task = _safe_ref_part(job_id)
    branch = f"codex/{instance}/{task}"
    if len(branch) > 240:
        branch = branch[:240].rstrip("./")
    if not branch or branch.endswith((".", "/")) or ".." in branch or "@{" in branch:
        raise ToolHandlerError(
            "generated code-task branch is invalid",
            error_code="code_task_branch_invalid",
            stage="preparing",
        )
    return branch


def _safe_ref_part(value: str) -> str:
    cleaned = _SAFE_REF_PART_RE.sub("-", str(value).strip()).strip(".-/")
    return cleaned or "task"


def _valid_ref_component(value: str) -> bool:
    return bool(
        value
        and not value.startswith(("-", ".", "/"))
        and not value.endswith((".", "/", ".lock"))
        and ".." not in value
        and "@{" not in value
        and "\\" not in value
        and not _CONTROL_RE.search(value)
    )


def _github_git_url(config: GitHubDeliveryConfig) -> str:
    return f"https://github.com/{config.repository}.git"


def _load_token_file(raw: str) -> tuple[Path, str]:
    if not raw:
        raise ToolHandlerError(
            f"{_TOKEN_FILE_ENV} is required",
            error_code="code_task_github_token_missing",
            stage="preparing",
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ToolHandlerError(
            "GitHub token file must be an absolute non-symlink path",
            error_code="code_task_github_token_invalid",
            stage="preparing",
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(candidate, flags)
    except OSError as exc:
        raise ToolHandlerError(
            "GitHub token file is unavailable",
            error_code="code_task_github_token_missing",
            stage="preparing",
        ) from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ToolHandlerError(
                "GitHub token file must be a single-link file owned by the "
                "worker user with mode 0600",
                error_code="code_task_github_token_permissions",
                stage="preparing",
            )
        try:
            stream = os.fdopen(fd, "r", encoding="utf-8")
            fd = -1
            with stream:
                token = stream.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolHandlerError(
                "GitHub token file is unavailable",
                error_code="code_task_github_token_missing",
                stage="preparing",
            ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(token) < 20 or any(char.isspace() for char in token):
        raise ToolHandlerError(
            "GitHub token file is empty or malformed",
            error_code="code_task_github_token_invalid",
            stage="preparing",
        )
    return candidate, token


@contextmanager
def _remote_git_environment(
    config: GitHubDeliveryConfig,
    job_dir: Path,
) -> Iterator[dict[str, str]]:
    token_fd, raw_token_path = tempfile.mkstemp(
        prefix="chatcopilot-github-token-"
    )
    token_path = Path(raw_token_path)
    helper_fd = -1
    helper: Path | None = None
    try:
        os.fchmod(token_fd, 0o600)
        token_stream = os.fdopen(token_fd, "w", encoding="utf-8")
        token_fd = -1
        with token_stream:
            token_stream.write(config.token + "\n")

        helper_fd, raw_helper_path = tempfile.mkstemp(
            prefix=".github-askpass.",
            dir=job_dir,
            text=True,
        )
        helper = Path(raw_helper_path)
        os.fchmod(helper_fd, 0o700)
        helper_stream = os.fdopen(helper_fd, "w", encoding="utf-8")
        helper_fd = -1
        with helper_stream:
            helper_stream.write(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  *sername*) printf "%s\\n" "x-access-token" ;;\n'
                f'  *assword*) /bin/cat "${{{_TOKEN_FILE_ENV}}}" ;;\n'
                "  *) exit 1 ;;\n"
                "esac\n"
            )

        env = _local_git_environment(job_dir)
        env.update(
            {
                "GIT_ASKPASS": str(helper),
                "GIT_TERMINAL_PROMPT": "0",
                _TOKEN_FILE_ENV: str(token_path),
            }
        )
        yield env
    finally:
        if helper_fd >= 0:
            os.close(helper_fd)
        if token_fd >= 0:
            os.close(token_fd)
        if helper is not None:
            helper.unlink(missing_ok=True)
        token_path.unlink(missing_ok=True)


def _local_git_environment(
    job_dir: Path,
    *,
    author_name: str = "",
    author_email: str = "",
) -> dict[str, str]:
    home = job_dir / "git-home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if author_name:
        env["GIT_AUTHOR_NAME"] = author_name
        env["GIT_COMMITTER_NAME"] = author_name
    if author_email:
        env["GIT_AUTHOR_EMAIL"] = author_email
        env["GIT_COMMITTER_EMAIL"] = author_email
    return env


def _git_output(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    config: GitHubDeliveryConfig | None = None,
    stage: str,
) -> str:
    return _run_git(
        args,
        cwd=cwd,
        env=env,
        config=config,
        stage=stage,
    ).stdout.strip()


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    config: GitHubDeliveryConfig | None = None,
    stage: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_REMOTE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolHandlerError(
            f"Git command failed: {type(exc).__name__}",
            error_code="code_task_git_command_failed",
            stage=stage,
        ) from exc
    if result.returncode != 0:
        detail = "\n".join(
            item.strip() for item in (result.stdout, result.stderr) if item.strip()
        )
        detail = _redact_git_detail(
            detail,
            cwd=cwd,
            args=args,
            env=env,
            config=config,
        )
        raise ToolHandlerError(
            f"Git command failed ({result.returncode}): {detail[-2000:]}",
            error_code="code_task_git_command_failed",
            stage=stage,
        )
    return result


def _redact_git_detail(
    detail: str,
    *,
    cwd: Path,
    args: Sequence[str],
    env: Mapping[str, str],
    config: GitHubDeliveryConfig | None,
) -> str:
    redacted = detail
    candidates = {str(cwd), str(cwd.resolve())}
    for raw in (*args, *env.values()):
        value = str(raw).strip()
        if value.startswith("file://"):
            value = value.removeprefix("file://")
        if not value or os.pathsep in value:
            continue
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            candidates.add(str(candidate))
            candidates.add(str(candidate.resolve(strict=False)))
    if config is not None:
        candidates.add(str(config.token_file))
        redacted = redacted.replace(config.token, "[REDACTED]")
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate and candidate != "/":
            redacted = redacted.replace(candidate, "[PATH]")
    return redacted


__all__ = [
    "DELIVERY_FILENAME",
    "VALIDATION_INDEX_FILENAME",
    "GitHubDeliveryConfig",
    "cleanup_validation_index",
    "compute_delivery_tree",
    "deliver_pull_request",
    "delivery_retry_pending",
    "load_delivery_config",
    "prepare_delivery_worktree",
    "prepare_validation_index",
    "validate_delivery_paths",
]
