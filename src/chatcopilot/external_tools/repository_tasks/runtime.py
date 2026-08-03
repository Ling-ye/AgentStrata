"""Managed Git clone and task-worktree lifecycle for repository tasks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterator, Sequence

from chatcopilot.external_tools.codebase.config import (
    CodeRepositoryConfig,
    CodebaseCheckConfig,
    codebase_cache_root,
    load_registry,
)
from chatcopilot.external_tools.codebase.path_guard import matches_any
from chatcopilot.project import ENV_PREFIX

_CHANGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,63}$")
_OUTPUT_LIMIT = 12_000
_MAX_PATCH_CHARS = 500_000
_LOCK_TIMEOUT_SECONDS = 60



@dataclass
class ChangeState:
    change_id: str
    repository_id: str
    branch: str
    worktree: str
    objective: str
    remote_url: str
    base_branch: str
    status: str = "prepared"
    base_sha: str = ""
    diff_hash: str = ""
    review_ok: bool = False
    review_hash: str = ""
    review_summary: str = ""
    checks: dict[str, dict] | None = None
    commit_sha: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if self.checks is None:
            self.checks = {}


def prepare_change(repository_id: str, objective: str, *, change_id: str = "") -> ChangeState:
    repository = load_registry().get(repository_id)
    _ensure_writable(repository)
    identifier = _normalize_change_id(change_id)
    if state_path(identifier).exists():
        raise ValueError(f"codebase change already exists: {identifier}")
    with repository_lock(repository.repository_id):
        remote_url = _repository_remote(repository)
        clone = _ensure_clone(repository, remote_url=remote_url)
        _git(clone, "fetch", "--prune", "origin", repository.base_branch, timeout=120)
        base_ref = f"refs/remotes/origin/{repository.base_branch}"
        base_sha = _git(clone, "rev-parse", base_ref).stdout.strip()
        worktree = _worktree_root(repository.repository_id) / identifier
        if worktree.exists():
            raise ValueError(f"managed worktree already exists: {identifier}")
        bot_id = _safe_segment(os.environ.get(f"{ENV_PREFIX}_BOT_ID", "chatcopilot"))
        branch = f"{repository.branch_prefix}/{bot_id}/{repository.repository_id}/{identifier}"
        _git(clone, "worktree", "add", "-b", branch, str(worktree), base_ref, timeout=120)
        state = ChangeState(
            change_id=identifier,
            repository_id=repository.repository_id,
            branch=branch,
            worktree=str(worktree),
            objective=objective.strip(),
            remote_url=remote_url,
            base_branch=repository.base_branch,
            base_sha=base_sha,
        )
        save_state(state)
        return state


def apply_change_patch(change_id: str, patch_text: str) -> ChangeState:
    state, repository, worktree = load_change(change_id)
    _require_status(
        state,
        {"prepared", "changed", "review_failed", "check_failed", "tested"},
    )
    if not patch_text.strip() or len(patch_text) > _MAX_PATCH_CHARS:
        raise ValueError(f"patch must contain 1-{_MAX_PATCH_CHARS} characters")
    _git(worktree, "apply", "--check", "--whitespace=nowarn", "-", input_text=patch_text)
    _git(worktree, "apply", "--whitespace=nowarn", "-", input_text=patch_text)
    try:
        _validate_changed_paths(repository, worktree)
    except Exception:
        _restore_worktree(worktree)
        raise
    _invalidate_gates(state, worktree)
    state.status = "changed"
    save_state(state)
    return state


def change_diff(change_id: str, *, max_chars: int = _OUTPUT_LIMIT) -> str:
    state, _, worktree = load_change(change_id)
    status = _git(worktree, "status", "--short").stdout.strip()
    diff = _git(worktree, "diff", "--no-ext-diff", "--unified=3", timeout=60).stdout
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    if untracked:
        snippets: list[str] = []
        for rel in untracked[:20]:
            target = worktree / rel
            try:
                body = target.read_text(encoding="utf-8", errors="replace")[:3000]
            except OSError:
                body = "<unreadable>"
            snippets.append(f"\n--- /dev/null\n+++ b/{rel}\n{body}")
        diff += "\n".join(snippets)
    rendered = f"# change={state.change_id} branch={state.branch}\n{status}\n\n{diff}".strip()
    return rendered[:max_chars] + ("\n...[truncated]" if len(rendered) > max_chars else "")


def record_review(change_id: str, *, ok: bool, summary: str) -> ChangeState:
    state, repository, worktree = load_change(change_id)
    _require_status(state, {"changed", "review_failed"})
    if not _validate_changed_paths(repository, worktree):
        raise ValueError("change has no modified files to review")
    state.diff_hash = _diff_hash(worktree)
    state.review_ok = bool(ok)
    state.review_hash = state.diff_hash
    state.review_summary = summary.strip()[:4000]
    state.status = "reviewed" if ok else "review_failed"
    save_state(state)
    return state


def run_checks(
    change_id: str,
    check_ids: Sequence[str] = (),
    *,
    _after_commit: bool = False,
) -> tuple[ChangeState, list[dict]]:
    state, repository, worktree = load_change(change_id)
    _require_status(
        state,
        {"committed"} if _after_commit else {"reviewed", "check_failed", "tested"},
    )
    _validate_changed_paths(repository, worktree)
    selected = _select_checks(repository, check_ids)
    if not selected:
        raise ValueError(f"repository {repository.repository_id!r} has no validation checks")
    current_hash = _diff_hash(worktree)
    if not _after_commit and (
        not state.review_ok or state.review_hash != current_hash
    ):
        raise ValueError("current diff has not passed code review")
    results: list[dict] = []
    for check in selected:
        started = time.monotonic()
        try:
            argv = list(check.argv)
            if argv and argv[0] == "python":
                argv[0] = sys.executable
            completed = subprocess.run(
                argv, cwd=str(worktree), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=check.timeout_seconds,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            output = (completed.stdout + "\n" + completed.stderr).strip()[-_OUTPUT_LIMIT:]
            result = {
                "id": check.check_id,
                "ok": completed.returncode == 0,
                "returncode": completed.returncode,
                "argv": argv,
                "seconds": round(time.monotonic() - started, 3),
                "output": output,
                "diff_hash": current_hash,
            }
        except subprocess.TimeoutExpired as exc:
            result = {
                "id": check.check_id, "ok": False, "returncode": -1,
                "argv": list(check.argv), "seconds": round(time.monotonic() - started, 3),
                "output": f"timeout after {check.timeout_seconds}s: {exc}", "diff_hash": current_hash,
            }
        state.checks[check.check_id] = result
        results.append(result)
    state.diff_hash = current_hash
    state.status = "tested" if all(item["ok"] for item in results) else "check_failed"
    save_state(state)
    return state, results



def publish_change_overlay(change_id: str) -> ChangeState:
    """Apply the exact validated diff to the configured local repository root."""

    state, repository, worktree = load_change(change_id)
    _require_status(state, {"tested"})
    changed = _validate_changed_paths(repository, worktree)
    if not changed:
        raise ValueError("change has no modified files")
    current_hash = _diff_hash(worktree)
    if not state.review_ok or state.review_hash != current_hash:
        raise ValueError("current diff has not passed code review")
    missing_checks = [
        check.check_id
        for check in repository.checks
        if not state.checks.get(check.check_id, {}).get("ok")
        or state.checks.get(check.check_id, {}).get("diff_hash") != current_hash
    ]
    if missing_checks:
        raise ValueError(
            f"current diff has not passed required checks: {', '.join(missing_checks)}"
        )
    missing_docs = [path for path in repository.required_docs if path not in changed]
    if missing_docs:
        raise ValueError(f"required documentation was not updated: {', '.join(missing_docs)}")

    target = repository.root.expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"repository overlay target is missing: {target}")
    target_head = _git(target, "rev-parse", "HEAD").stdout.strip()
    if target_head != state.base_sha:
        raise RuntimeError("repository overlay target changed since task preparation")
    dirty = set(_validate_changed_paths(repository, target))
    overlap = sorted(dirty.intersection(changed))
    if overlap:
        raise RuntimeError(
            "repository overlay conflicts with existing target changes: " + ", ".join(overlap)
        )
    _git(worktree, "add", "--intent-to-add", "--", ".")
    patch = _git(
        worktree,
        "diff",
        "--binary",
        "--full-index",
        "HEAD",
        "--",
        timeout=120,
    ).stdout
    if not patch:
        raise RuntimeError("repository task produced no attributable patch")
    with repository_lock(repository.repository_id):
        _git(target, "apply", "--check", "--binary", "-", input_text=patch, timeout=120)
        _git(target, "apply", "--binary", "-", input_text=patch, timeout=120)
    state.status = "published"
    state.error = ""
    save_state(state)
    return state


def abort_change(change_id: str) -> ChangeState:
    state = load_state(change_id)
    if state.status == "aborted":
        return state
    repository = load_registry().get(state.repository_id)
    worktree = Path(state.worktree).expanduser().resolve()
    expected = _worktree_root(repository.repository_id).resolve()
    try:
        worktree.relative_to(expected)
    except ValueError as exc:
        raise PermissionError("change worktree escaped the managed cache root") from exc
    clone = _clone_root(repository.repository_id)
    if worktree.exists():
        _git(clone, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)
    state.status = "aborted"
    state.error = ""
    save_state(state)
    return state


def load_change(change_id: str) -> tuple[ChangeState, CodeRepositoryConfig, Path]:
    state = load_state(change_id)
    repository = load_registry().get(state.repository_id)
    if repository.base_branch != state.base_branch:
        raise ValueError(
            f"repository base_branch changed during task {change_id}: "
            f"{state.base_branch!r} -> {repository.base_branch!r}"
        )
    current_remote = _repository_remote(repository)
    if current_remote != state.remote_url:
        raise ValueError(f"repository remote changed during task {change_id}")
    worktree = Path(state.worktree).resolve()
    expected_root = _worktree_root(repository.repository_id).resolve()
    try:
        worktree.relative_to(expected_root)
    except ValueError as exc:
        raise PermissionError("change worktree escaped the managed cache root") from exc
    if not worktree.is_dir():
        raise FileNotFoundError(f"managed worktree is missing for change {change_id}")
    return state, repository, worktree


def change_repository(change_id: str) -> CodeRepositoryConfig:
    _, repository, worktree = load_change(change_id)
    return replace(repository, root=worktree)


def load_state(change_id: str) -> ChangeState:
    path = state_path(change_id)
    if not path.is_file():
        raise FileNotFoundError(f"unknown codebase change: {change_id}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ChangeState(**raw)


def save_state(state: ChangeState) -> None:
    path = state_path(state.change_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def state_path(change_id: str) -> Path:
    if not _CHANGE_ID_RE.fullmatch(change_id):
        raise ValueError(f"invalid codebase change id: {change_id!r}")
    return codebase_cache_root() / "tasks" / f"{change_id}.json"


def _ensure_clone(repository: CodeRepositoryConfig, *, remote_url: str) -> Path:
    clone = _clone_root(repository.repository_id)
    if (clone / ".git").is_dir():
        cached_remote = _git(clone, "remote", "get-url", "origin").stdout.strip()
        if cached_remote != remote_url:
            raise ValueError(
                f"managed clone remote mismatch for {repository.repository_id!r}; "
                "remove its codebase cache before changing remotes"
            )
        return clone
    clone.parent.mkdir(parents=True, exist_ok=True)
    _run([_git_executable(), "clone", "--no-checkout", remote_url, str(clone)], timeout=180)
    return clone


def _repository_remote(repository: CodeRepositoryConfig) -> str:
    remote = repository.remote
    if remote is None:
        remote = _git(repository.root, "remote", "get-url", "origin").stdout.strip()
    if not remote:
        raise ValueError(f"repository {repository.repository_id!r} has no remote")
    return remote


def _validate_changed_paths(repository: CodeRepositoryConfig, worktree: Path) -> list[str]:
    tracked = _git(worktree, "diff", "--no-ext-diff", "--name-only", "HEAD").stdout.splitlines()
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    changed = sorted({path.replace("\\", "/") for path in (*tracked, *untracked) if path.strip()})
    for path in changed:
        if matches_any(path, repository.deny_globs):
            raise PermissionError(f"changed path is denied: {path}")
        if repository.write_globs and not matches_any(path, repository.write_globs):
            raise PermissionError(f"changed path is outside write_globs: {path}")
    return changed


def _invalidate_gates(state: ChangeState, worktree: Path) -> None:
    state.diff_hash = _diff_hash(worktree)
    state.review_ok = False
    state.review_hash = ""
    state.review_summary = ""
    state.checks = {}


def _diff_hash(worktree: Path) -> str:
    diff = _git(worktree, "diff", "--binary", "HEAD").stdout
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    hasher = hashlib.sha256(diff.encode("utf-8", errors="replace"))
    for rel in sorted(untracked):
        hasher.update(rel.encode("utf-8"))
        try:
            hasher.update((worktree / rel).read_bytes())
        except OSError:
            pass
    return hasher.hexdigest()


def _select_checks(
    repository: CodeRepositoryConfig, check_ids: Sequence[str]
) -> tuple[CodebaseCheckConfig, ...]:
    if not check_ids:
        return repository.checks
    requested = {str(item) for item in check_ids}
    selected = tuple(check for check in repository.checks if check.check_id in requested)
    missing = requested - {check.check_id for check in selected}
    if missing:
        raise ValueError(f"unknown validation checks: {', '.join(sorted(missing))}")
    return selected


def _restore_worktree(worktree: Path) -> None:
    _git(worktree, "restore", "--source=HEAD", "--staged", "--worktree", "--", ".", check=False)
    _git(worktree, "clean", "-fd", check=False)


def _ensure_writable(repository: CodeRepositoryConfig) -> None:
    if not repository.write_enabled:
        raise PermissionError(f"repository {repository.repository_id!r} is read-only")
    if not repository.checks:
        raise ValueError(f"writable repository {repository.repository_id!r} must declare checks")


def _require_status(state: ChangeState, allowed: set[str]) -> None:
    if state.status not in allowed:
        raise ValueError(f"change {state.change_id} cannot be edited in status {state.status!r}")


def _normalize_change_id(value: str) -> str:
    candidate = value.strip().lower() if value else f"task-{uuid.uuid4().hex[:12]}"
    if not _CHANGE_ID_RE.fullmatch(candidate):
        raise ValueError("change_id must use 6-64 lowercase letters, digits, and hyphens")
    return candidate


def _safe_segment(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return normalized or "chatcopilot"


def _clone_root(repository_id: str) -> Path:
    return codebase_cache_root() / "repositories" / repository_id / "clone"


def _worktree_root(repository_id: str) -> Path:
    return codebase_cache_root() / "repositories" / repository_id / "worktrees"


@contextmanager
def repository_lock(repository_id: str) -> Iterator[None]:
    lock = codebase_cache_root() / "locks" / f"{repository_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 600:
                    shutil.rmtree(lock, ignore_errors=True)
                    continue
            except OSError:
                pass
            if time.monotonic() - started > _LOCK_TIMEOUT_SECONDS:
                raise TimeoutError(f"timed out waiting for repository lock: {repository_id}")
            time.sleep(0.2)
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _git(
    cwd: Path, *args: str, input_text: str | None = None, check: bool = True, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return _run([_git_executable(), "-C", str(cwd), *args], input_text=input_text, check=check, timeout=timeout)


def _run(
    argv: list[str], *, input_text: str | None = None, check: bool = True, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv, input=input_text, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    if check and completed.returncode != 0:
        detail = (completed.stdout + "\n" + completed.stderr).strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {argv[0]} {argv[1]}\n{detail[-4000:]}")
    return completed


def _git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("git is required for codebase changes")
    return executable


__all__ = [
    "ChangeState",
    "abort_change",
    "apply_change_patch",
    "change_diff",
    "change_repository",
    "load_change",
    "load_state",
    "prepare_change",
    "publish_change_overlay",
    "record_review",
    "run_checks",
    "save_state",
    "state_path",
]
