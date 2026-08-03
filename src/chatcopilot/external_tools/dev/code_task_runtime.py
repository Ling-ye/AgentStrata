"""Isolated Owner code-task execution, validation, and PR delivery."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.contracts.code_tasks import (
    CodeTaskLimits,
    validate_code_task_title,
)
from chatcopilot.contracts.model_selection import CODEX_REASONING_EFFORTS
from chatcopilot.contracts.tools import HandlerResult, ToolContext, ToolHandlerError
from chatcopilot.core.jobs import (
    code_task_state_lock,
    read_json_file,
    write_job_status,
    write_json_atomic,
)
from chatcopilot.core.source_manifest import filter_source_paths
from chatcopilot.external_tools.codex_cli.credentials import (
    CredentialError,
    credential_lease,
    validate_auth_root_path,
)
from chatcopilot.external_tools.dev.code_task_delivery import (
    compute_delivery_tree,
    deliver_pull_request,
    delivery_retry_pending,
    prepare_delivery_worktree,
    validate_delivery_paths,
)
from chatcopilot.project import ENV_PREFIX

CHANGES_FILENAME = "changes.json"
CODEX_EVENTS_FILENAME = "codex-events.jsonl"
CODEX_SESSION_FILENAME = "codex-session.json"
SUPERVISOR_FILENAME = "supervisor.json"
DISPATCH_FILENAME = "dispatch.json"
VALIDATION_FILENAME = "validation.json"
_HEAVY_RETAINED_NAMES = frozenset(
    {
        "task-home",
        "validation-home",
        "worktree",
    }
)

@dataclass(frozen=True)
class CodeTaskPaths:
    job_dir: Path
    source_root: Path
    worktree: Path
    task_home: Path
    validation_home: Path

    @classmethod
    def build(cls, *, job_dir: Path, source_root: Path) -> "CodeTaskPaths":
        return cls(
            job_dir=job_dir,
            source_root=source_root,
            worktree=job_dir / "worktree",
            task_home=job_dir / "task-home",
            validation_home=job_dir / "validation-home",
        )


@dataclass(frozen=True)
class CodeTaskChange:
    path: str
    kind: str
    before_hash: str | None
    after_hash: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
        }


def execute_code_task(args: dict[str, Any], ctx: ToolContext) -> HandlerResult:
    if ctx.job is None:
        raise ToolHandlerError(
            "code task requires a background job context",
            error_code="code_task_context_missing",
            stage="preparing",
        )
    prompt = str(args.get("prompt") or "").strip()
    job_dir = Path(ctx.job.job_dir).resolve()
    delivery_only = bool(args.get("delivery_only")) or delivery_retry_pending(
        job_dir
    )
    if not prompt and not delivery_only:
        raise ToolHandlerError(
            "prompt is required",
            error_code="code_task_prompt_missing",
            stage="preparing",
        )
    title = validate_code_task_title(str(args.get("title") or ""))
    acceptance = tuple(
        str(item).strip()
        for item in (args.get("acceptance_criteria") or [])
        if str(item).strip()
    )
    job_dir.chmod(0o700)
    source_root = _source_root()
    paths = CodeTaskPaths.build(job_dir=job_dir, source_root=source_root)
    limits = code_task_limits()

    try:
        if delivery_only:
            return _resume_delivery_only(paths=paths, ctx=ctx, title=title)
        _check_cancel(paths)
        _set_stage(ctx, "preparing", "Preparing isolated GitHub clone.")
        _prepare_task(paths)
        auth_root = _worker_credential_root()
        try:
            with credential_lease(
                auth_root,
                "worker",
                paths.task_home / ".codex",
            ) as lease:
                session = read_json_file(job_dir / CODEX_SESSION_FILENAME) or {}
                native_session_id = str(session.get("native_session_id") or "")
                session_generation = session.get("credential_generation")
                generation_matches = (
                    isinstance(session_generation, int)
                    and not isinstance(session_generation, bool)
                    and session_generation == lease.generation
                )
                if native_session_id and not generation_matches:
                    native_session_id = ""
                    _write_codex_session(
                        job_dir,
                        native_session_id="",
                        credential_generation=lease.generation,
                    )
                rendered_prompt = _render_prompt(
                    prompt,
                    acceptance,
                    resume=bool(native_session_id),
                )
                _set_stage(ctx, "running", "Codex is editing the isolated clone.")
                final_text, native_session_id = _run_codex_stream(
                    paths,
                    rendered_prompt,
                    native_session_id=native_session_id,
                    limits=limits,
                )
                _write_codex_session(
                    job_dir,
                    native_session_id=native_session_id,
                    credential_generation=lease.generation,
                )
        except CredentialError as exc:
            raise ToolHandlerError(
                (
                    "Codex worker authentication is unavailable; run "
                    "`python -m chatcopilot bot codex-auth login "
                    "--bot <bot.yaml> --lane worker`."
                ),
                error_code="code_task_codex_auth_unavailable",
                stage="preparing",
                details={"credential_error_code": exc.code},
            ) from exc

        changes = _task_changes(paths)
        validate_delivery_paths(
            [change.path for change in changes],
            stage="validating",
        )
        pre_validation_tree = (
            compute_delivery_tree(
                job_dir=job_dir,
                worktree=paths.worktree,
                changed_files=[change.path for change in changes],
                stage="validating",
            )
            if changes
            else ""
        )
        write_json_atomic(
            job_dir / CHANGES_FILENAME,
            {
                "files": [change.to_payload() for change in changes],
                "updated_at": time.time(),
            },
        )
        _set_stage(
            ctx,
            "validating",
            f"Running validation for {len(changes)} changed file(s).",
            details={"changed_files": [change.path for change in changes]},
        )
        checks = _validate_task(paths, changes, limits=limits)
        post_validation_changes = _task_changes(paths)
        if post_validation_changes != changes:
            raise ToolHandlerError(
                "validation changed the source delta",
                error_code="code_task_validation_mutated_source",
                stage="validating",
                details={
                    "before": [change.path for change in changes],
                    "after": [
                        change.path for change in post_validation_changes
                    ],
                },
            )
        validated_tree_sha = ""
        if changes:
            validated_tree_sha = compute_delivery_tree(
                job_dir=job_dir,
                worktree=paths.worktree,
                changed_files=[change.path for change in changes],
                stage="validating",
            )
            if validated_tree_sha != pre_validation_tree:
                raise ToolHandlerError(
                    "validation changed the Git tree",
                    error_code="code_task_validation_mutated_source",
                    stage="validating",
                )
            validation_payload = (
                read_json_file(job_dir / VALIDATION_FILENAME) or {}
            )
            write_json_atomic(
                job_dir / VALIDATION_FILENAME,
                {
                    **validation_payload,
                    "validated_tree_sha": validated_tree_sha,
                },
            )

        delivery: dict[str, Any] = {"delivered": False}
        if changes:
            with code_task_state_lock(job_dir):
                _check_cancel(paths)
                _set_stage(ctx, "delivering", "Committing and opening a draft PR.")
            delivery = deliver_pull_request(
                job_dir=job_dir,
                worktree=paths.worktree,
                title=title,
                changed_files=[change.path for change in changes],
                checks=checks,
                validated_tree_sha=validated_tree_sha,
            )

        summary_payload = {
            "task_id": ctx.job.job_id,
            "changed_files": [change.path for change in changes],
            "checks": checks,
            "delivered": bool(delivery.get("delivered")),
            "branch": str(delivery.get("branch") or ""),
            "commit_sha": str(delivery.get("commit_sha") or ""),
            "pr_url": str(delivery.get("pr_url") or ""),
            "pr_number": delivery.get("pr_number"),
            "draft": delivery.get("draft"),
            "final": final_text[-2000:],
        }
        _cleanup_success(paths)
        return json.dumps(summary_payload, ensure_ascii=False), [str(job_dir)], None
    except ToolHandlerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolHandlerError(
            f"{type(exc).__name__}: {exc}",
            error_code="code_task_failed",
            stage=str(
                (read_json_file(job_dir / "status.json") or {}).get("stage")
                or "running"
            ),
        ) from exc


def _resume_delivery_only(
    *,
    paths: CodeTaskPaths,
    ctx: ToolContext,
    title: str,
) -> HandlerResult:
    changes_payload = read_json_file(paths.job_dir / CHANGES_FILENAME) or {}
    raw_files = changes_payload.get("files")
    changed_files = [
        str(item.get("path"))
        for item in (raw_files if isinstance(raw_files, list) else [])
        if isinstance(item, dict) and item.get("path")
    ]
    validation = read_json_file(paths.job_dir / VALIDATION_FILENAME) or {}
    raw_checks = validation.get("checks")
    checks = [
        str(item)
        for item in (raw_checks if isinstance(raw_checks, list) else [])
    ]
    validated_tree_sha = str(validation.get("validated_tree_sha") or "")
    if not changed_files or not delivery_retry_pending(paths.job_dir):
        raise ToolHandlerError(
            "code task has no pending PR delivery",
            error_code="code_task_delivery_not_pending",
            stage="preparing",
        )
    with code_task_state_lock(paths.job_dir):
        _check_cancel(paths)
        _set_stage(ctx, "delivering", "Retrying draft PR delivery.")
    delivery = deliver_pull_request(
        job_dir=paths.job_dir,
        worktree=paths.worktree,
        title=title,
        changed_files=changed_files,
        checks=checks,
        validated_tree_sha=validated_tree_sha,
    )
    summary_payload = {
        "task_id": ctx.job.job_id,
        "changed_files": changed_files,
        "checks": checks,
        "delivered": bool(delivery.get("delivered")),
        "branch": str(delivery.get("branch") or ""),
        "commit_sha": str(delivery.get("commit_sha") or ""),
        "pr_url": str(delivery.get("pr_url") or ""),
        "pr_number": delivery.get("pr_number"),
        "draft": delivery.get("draft"),
        "final": "",
    }
    _cleanup_success(paths)
    return (
        json.dumps(summary_payload, ensure_ascii=False),
        [str(paths.job_dir)],
        None,
    )

def code_task_limits() -> CodeTaskLimits:
    defaults = CodeTaskLimits()
    return CodeTaskLimits(
        timeout_seconds=_env_int(
            f"{ENV_PREFIX}_CODE_TASK_TIMEOUT_SECONDS", defaults.timeout_seconds, minimum=60
        ),
        memory_max_bytes=_env_int(
            f"{ENV_PREFIX}_CODE_TASK_MEMORY_MAX_BYTES",
            defaults.memory_max_bytes,
            minimum=256 * 1024**2,
        ),
        cpu_quota_percent=_env_int(
            f"{ENV_PREFIX}_CODE_TASK_CPU_QUOTA_PERCENT",
            defaults.cpu_quota_percent,
            minimum=100,
        ),
        tasks_max=_env_int(
            f"{ENV_PREFIX}_CODE_TASK_TASKS_MAX", defaults.tasks_max, minimum=32
        ),
        active_disk_max_bytes=_env_int(
            f"{ENV_PREFIX}_CODE_TASK_DISK_MAX_BYTES",
            defaults.active_disk_max_bytes,
            minimum=256 * 1024**2,
        ),
        heartbeat_seconds=_env_int(
            f"{ENV_PREFIX}_CODE_TASK_HEARTBEAT_SECONDS",
            defaults.heartbeat_seconds,
            minimum=5,
        ),
        progress_notify_seconds=_env_int(
            f"{ENV_PREFIX}_CODE_TASK_PROGRESS_NOTIFY_SECONDS",
            defaults.progress_notify_seconds,
            minimum=30,
        ),
        cancel_grace_seconds=_env_int(
            f"{ENV_PREFIX}_CODE_TASK_CANCEL_GRACE_SECONDS",
            defaults.cancel_grace_seconds,
            minimum=1,
        ),
    )


def build_bwrap_command(
    paths: CodeTaskPaths,
    command: list[str],
    *,
    include_codex: bool,
    root: Path | None = None,
) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise ToolHandlerError(
            "bubblewrap is required for code tasks",
            error_code="code_task_bwrap_missing",
            stage="preparing",
        )
    mounted_root = (root or paths.worktree).resolve()
    home = paths.task_home if include_codex else paths.validation_home
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/run",
        "--dir",
        "/var",
        "--dir",
        "/var/tmp",
        "--dir",
        "/sandbox-home",
        "--dir",
        "/etc",
        "--bind",
        str(home),
        "/sandbox-home/agent",
        "--bind",
        str(mounted_root),
        "/workspace",
        "--chdir",
        "/workspace",
    ]
    for system_path in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(system_path).exists():
            argv.extend(["--ro-bind", system_path, system_path])
    for system_path in (
        "/etc/ca-certificates",
        "/etc/group",
        "/etc/hosts",
        "/etc/ld.so.cache",
        "/etc/localtime",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/resolv.conf",
        "/etc/ssl",
    ):
        if Path(system_path).exists():
            argv.extend(["--ro-bind", system_path, system_path])

    toolchain = paths.source_root / ".venv"
    if toolchain.is_dir():
        argv.extend(["--dir", "/toolchain", "--ro-bind", str(toolchain), "/toolchain/venv"])
    node_modules = paths.source_root / "console" / "node_modules"
    if node_modules.is_dir() and (mounted_root / "console").is_dir():
        argv.extend(
            [
                "--ro-bind",
                str(node_modules),
                "/workspace/console/node_modules",
            ]
        )

    git_metadata = mounted_root / ".git"
    if git_metadata.is_dir():
        argv.extend(["--ro-bind", str(git_metadata), "/workspace/.git"])

    path_entries = ["/usr/local/bin", "/usr/bin", "/bin"]
    if toolchain.is_dir():
        path_entries.insert(0, "/toolchain/venv/bin")
    if include_codex:
        codex_bin = _codex_binary()
        argv.extend(
            [
                "--dir",
                "/opt",
                "--dir",
                "/opt/codex",
                "--ro-bind",
                str(codex_bin),
                "/opt/codex/codex",
            ]
        )
        path_entries.insert(0, "/opt/codex")

    env = {
        "HOME": "/sandbox-home/agent",
        "PATH": ":".join(path_entries),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": "/workspace/src",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if include_codex:
        env["CODEX_HOME"] = "/sandbox-home/agent/.codex"
    for name, value in env.items():
        argv.extend(["--setenv", name, value])
    argv.append("--")
    argv.extend(command)
    return argv


def _prepare_task(paths: CodeTaskPaths) -> None:
    instance_id = os.environ.get(f"{ENV_PREFIX}_INSTANCE_ID", "").strip()
    if not instance_id:
        raise ToolHandlerError(
            f"{ENV_PREFIX}_INSTANCE_ID is required",
            error_code="code_task_instance_missing",
            stage="preparing",
        )
    prepare_delivery_worktree(
        job_dir=paths.job_dir,
        worktree=paths.worktree,
        instance_id=instance_id,
        source_root=paths.source_root,
    )

def _worker_credential_root() -> Path:
    raw = os.environ.get(f"{ENV_PREFIX}_CODEX_BOT_HOME", "").strip()
    if not raw:
        raise ToolHandlerError(
            f"{ENV_PREFIX}_CODEX_BOT_HOME is required",
            error_code="code_task_dedicated_auth_missing",
            stage="preparing",
        )
    try:
        return validate_auth_root_path(raw)
    except CredentialError as exc:
        if exc.code == "auth_root_personal_forbidden":
            error_code = "code_task_personal_auth_forbidden"
            message = "dedicated Codex home must differ from personal Codex home"
        else:
            error_code = "code_task_dedicated_auth_invalid"
            message = "dedicated Codex home must be a valid absolute private path"
        raise ToolHandlerError(
            message,
            error_code=error_code,
            stage="preparing",
        ) from exc


def _write_codex_session(
    job_dir: Path,
    *,
    native_session_id: str,
    credential_generation: int,
) -> None:
    write_json_atomic(
        job_dir / CODEX_SESSION_FILENAME,
        {
            "native_session_id": native_session_id,
            "credential_generation": credential_generation,
            "updated_at": time.time(),
        },
    )


def _run_codex_stream(
    paths: CodeTaskPaths,
    prompt: str,
    *,
    native_session_id: str,
    limits: CodeTaskLimits,
) -> tuple[str, str]:
    model = os.environ.get(f"{ENV_PREFIX}_CODE_MODEL", "").strip()
    effort = os.environ.get(
        f"{ENV_PREFIX}_CODE_REASONING_EFFORT", ""
    ).strip().lower()
    if not model or effort not in CODEX_REASONING_EFFORTS:
        raise ToolHandlerError(
            "code-worker model policy was not derived from BotSpec",
            error_code="code_task_model_policy_invalid",
            stage="preparing",
        )
    codex_args = [
        "/opt/codex/codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "-m",
        model,
        "-C",
        "/workspace",
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        "mcp_servers={}",
        "-c",
        "features.hooks=false",
        "-c",
        'web_search="live"',
    ]
    if native_session_id:
        codex_args.extend(["resume", native_session_id, "-"])
    else:
        codex_args.append("-")
    command = build_bwrap_command(paths, codex_args, include_codex=True)
    stderr_path = paths.job_dir / "codex-stderr.log"
    started = time.monotonic()
    final_parts: list[str] = []
    native_id = native_session_id
    with stderr_path.open("a", encoding="utf-8") as stderr:
        process = subprocess.Popen(  # noqa: S603 - argv is internally constructed
            command,
            cwd=str(paths.source_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        write_json_atomic(
            paths.job_dir / SUPERVISOR_FILENAME,
            {
                "pid": process.pid,
                "pgid": os.getpgid(process.pid),
                "proc_start_ticks": _process_start_ticks(process.pid),
                "boot_id": _boot_id(),
                "started_at": time.time(),
                "unit": os.environ.get(f"{ENV_PREFIX}_CODE_TASK_SYSTEMD_UNIT", ""),
            },
        )
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        last_heartbeat = 0.0
        try:
            while True:
                _check_cancel(paths, process=process, limits=limits)
                elapsed = time.monotonic() - started
                if elapsed > limits.timeout_seconds:
                    _terminate_process_group(process, limits.cancel_grace_seconds)
                    raise ToolHandlerError(
                        "code task timed out",
                        error_code="code_task_timeout",
                        stage="running",
                    )
                if elapsed - last_heartbeat >= limits.heartbeat_seconds:
                    resource = _resource_sample(process.pid, paths.job_dir)
                    if int(resource.get("disk_bytes") or 0) > limits.active_disk_max_bytes:
                        _terminate_process_group(process, limits.cancel_grace_seconds)
                        raise ToolHandlerError(
                            "code task exceeded active disk limit",
                            error_code="code_task_disk_limit",
                            stage="running",
                            details=resource,
                        )
                    write_job_status(
                        paths.job_dir,
                        "running",
                        "Codex is running.",
                        stage="running",
                        heartbeat_at=time.time(),
                        resource=resource,
                    )
                    last_heartbeat = elapsed
                ready = selector.select(timeout=1.0)
                for key, _ in ready:
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    event = _consume_codex_event(paths, line)
                    if event is None:
                        continue
                    event_type = str(event.get("type") or "")
                    if event_type in {"thread.started", "thread_started"}:
                        native_id = str(
                            event.get("thread_id")
                            or event.get("threadId")
                            or event.get("id")
                            or native_id
                        ).strip()
                    item = event.get("item") if isinstance(event.get("item"), dict) else event
                    if event_type in {"item.completed", "item_completed"} and str(
                        item.get("type") or ""
                    ) in {"agent_message", "message"}:
                        text = str(item.get("text") or item.get("content") or "").strip()
                        if text:
                            final_parts.append(text)
                if process.poll() is not None:
                    for line in process.stdout:
                        event = _consume_codex_event(paths, line)
                        if event is None:
                            continue
                        if str(event.get("type") or "") in {"thread.started", "thread_started"}:
                            native_id = str(
                                event.get("thread_id")
                                or event.get("threadId")
                                or event.get("id")
                                or native_id
                            ).strip()
                    break
        finally:
            selector.close()
    if process.returncode != 0:
        tail = _tail_text(stderr_path)
        raise _codex_process_error(process.returncode, tail)
    return "\n".join(final_parts).strip(), native_id


def _validate_task(
    paths: CodeTaskPaths,
    changes: tuple[CodeTaskChange, ...],
    *,
    limits: CodeTaskLimits,
) -> list[str]:
    if not changes:
        payload = {"checks": [], "status": "passed", "reason": "no source changes"}
        write_json_atomic(paths.job_dir / VALIDATION_FILENAME, payload)
        return []
    quick = os.environ.get(
        f"{ENV_PREFIX}_CODE_TASK_QUICK_COMMAND", "git diff --check"
    ).strip()
    full = os.environ.get(
        f"{ENV_PREFIX}_CODE_TASK_FULL_COMMAND",
        "/toolchain/venv/bin/python scripts/check_repo.py full",
    ).strip()
    checks: list[str] = []
    for name, command in (("quick", quick), ("full", full)):
        _check_cancel(paths)
        _run_validation_command(
            paths,
            root=paths.worktree,
            name=name,
            command=command,
            timeout_seconds=limits.timeout_seconds,
        )
        checks.append(name)
    write_json_atomic(
        paths.job_dir / VALIDATION_FILENAME,
        {
            "status": "passed",
            "checks": checks,
            "changed_files": [change.path for change in changes],
            "finished_at": time.time(),
        },
    )
    return checks


def _run_validation_command(
    paths: CodeTaskPaths,
    *,
    root: Path,
    name: str,
    command: str,
    timeout_seconds: int,
) -> None:
    if not command:
        raise ToolHandlerError(
            f"{name} validation command is empty",
            error_code="code_task_validation_config",
            stage="validating",
        )
    argv = build_bwrap_command(
        paths,
        ["/bin/bash", "-lc", command],
        include_codex=False,
        root=root,
    )
    output = paths.job_dir / f"validation-{name}.log"
    started = time.monotonic()
    with output.open("a", encoding="utf-8") as stream:
        process = subprocess.Popen(  # noqa: S603 - configured validation command
            argv,
            cwd=str(paths.source_root),
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        while process.poll() is None:
            _check_cancel(paths, process=process, limits=code_task_limits())
            if time.monotonic() - started > timeout_seconds:
                _terminate_process_group(process, code_task_limits().cancel_grace_seconds)
                raise ToolHandlerError(
                    f"{name} validation timed out",
                    error_code="code_task_validation_timeout",
                    stage="validating",
                )
            time.sleep(0.5)
    if process.returncode != 0:
        raise ToolHandlerError(
            f"{name} validation failed: {_tail_text(output)}",
            error_code="code_task_validation_failed",
            stage="validating",
            details={"check": command},
        )


def _task_changes(paths: CodeTaskPaths) -> tuple[CodeTaskChange, ...]:
    tracked_raw = _git_output(
        paths.worktree,
        ["diff", "--name-only", "--no-renames", "-z", "HEAD", "--"],
    )
    untracked_raw = _git_output(
        paths.worktree,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    tracked = {item for item in tracked_raw.split("\0") if item}
    untracked = {item for item in untracked_raw.split("\0") if item}
    raw_paths = tracked | untracked
    selected = set(filter_source_paths(raw_paths))
    rejected = sorted(raw_paths - selected)
    if rejected:
        raise ToolHandlerError(
            "code task produced unsafe or non-deployable paths",
            error_code="code_task_scope_violation",
            stage="validating",
            details={"violating_files": rejected},
        )

    changes: list[CodeTaskChange] = []
    for rel in sorted(selected):
        candidate = paths.worktree / rel
        after = (
            _hash_path(candidate)
            if candidate.exists() or candidate.is_symlink()
            else None
        )
        if rel in untracked:
            kind = "created"
            before = None
        elif after is None:
            kind = "deleted"
            before = _git_output(paths.worktree, ["rev-parse", f"HEAD:{rel}"])
        else:
            kind = "modified"
            before = _git_output(paths.worktree, ["rev-parse", f"HEAD:{rel}"])
        changes.append(CodeTaskChange(rel, kind, before, after))
    return tuple(changes)

def _consume_codex_event(paths: CodeTaskPaths, line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    with (paths.job_dir / CODEX_EVENTS_FILENAME).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return event


def _render_prompt(
    prompt: str,
    acceptance: tuple[str, ...],
    *,
    resume: bool,
) -> str:
    criteria = "\n".join(f"- {item}" for item in acceptance) or "- Infer testable criteria."
    prefix = (
        "Continue the retained task in the same worktree."
        if resume
        else "Implement this task in the isolated repository worktree."
    )
    return f"""\
{prefix}

Follow AGENTS.md and Spec-Driven Development. For architecture, public-contract,
deployment, or migration changes, create or update specs before implementation.
Inspect existing patterns before editing. Add regression tests and update affected
documentation after code. Run relevant checks and fix failures. Do not commit,
push, create a pull request, access credentials, or modify files outside this
worktree.

Acceptance criteria:
{criteria}

User request:
{prompt}
"""


def _check_cancel(
    paths: CodeTaskPaths,
    *,
    process: subprocess.Popen[str] | None = None,
    limits: CodeTaskLimits | None = None,
) -> None:
    if not (paths.job_dir / "cancel-request.json").is_file():
        return
    if process is not None and process.poll() is None:
        _terminate_process_group(process, (limits or code_task_limits()).cancel_grace_seconds)
    write_job_status(
        paths.job_dir,
        "cancelled",
        "Code task cancelled.",
        stage="cancelled",
        error_code="code_task_cancelled",
    )
    raise ToolHandlerError(
        "code task cancelled",
        error_code="code_task_cancelled",
        stage="cancelled",
    )


def _terminate_process_group(
    process: subprocess.Popen[str],
    grace_seconds: int,
) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def terminate_recorded_task(job_dir: Path, *, grace_seconds: int = 10) -> bool:
    supervisor = read_json_file(job_dir / SUPERVISOR_FILENAME) or {}
    dispatch = read_json_file(job_dir / DISPATCH_FILENAME) or {}
    record = supervisor or dispatch
    if str(record.get("boot_id") or "") != _boot_id():
        return False
    unit = str(supervisor.get("unit") or "").strip()
    if not unit and str(dispatch.get("kind") or "") == "systemd":
        unit = str(dispatch.get("worker") or "").strip()
    if unit and shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "--user", "kill", "--signal=TERM", unit],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    try:
        pid = int(supervisor.get("pid") or dispatch.get("pid") or 0)
        pgid = int(supervisor.get("pgid") or pid)
    except (TypeError, ValueError):
        pid = 0
        pgid = 0
    if pgid <= 1:
        return False
    try:
        expected_ticks = int(
            supervisor.get("proc_start_ticks")
            or dispatch.get("proc_start_ticks")
            or 0
        )
    except (TypeError, ValueError):
        return False
    if pid <= 1 or (
        expected_ticks and _process_start_ticks(pid) != expected_ticks
    ):
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + max(1, grace_seconds)
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def schedule_code_task_worker(request_path: Path) -> str:
    """Start a code-task worker outside the QQ bot service cgroup."""
    request_path = request_path.expanduser().resolve()
    if not request_path.is_file():
        raise RuntimeError(f"code task request is missing: {request_path}")
    request = read_json_file(request_path) or {}
    job_id = str(request.get("job_id") or request_path.parent.name)
    dispatch = read_json_file(request_path.parent / DISPATCH_FILENAME) or {}
    if _dispatch_is_active(dispatch):
        return str(dispatch.get("worker") or "")
    status = read_json_file(request_path.parent / "status.json") or {}
    attempt = int(status.get("attempt") or 1)
    unit = f"chatcopilot-code-task-{job_id}-a{attempt}"
    source_root = _source_root()
    pythonpath = str(source_root / "src")
    worker = [
        sys.executable,
        "-m",
        "chatcopilot.middleware.runtime.jobs.worker",
        str(request_path),
    ]
    if shutil.which("systemd-run") and not _env_bool(
        f"{ENV_PREFIX}_CODE_TASK_DISABLE_SYSTEMD", False
    ):
        limits = code_task_limits()
        command = [
            "systemd-run",
            "--user",
            "--collect",
            f"--unit={unit}",
            f"--property=WorkingDirectory={source_root}",
            "--property=MemoryHigh=2G",
            f"--property=MemoryMax={limits.memory_max_bytes}",
            f"--property=CPUQuota={limits.cpu_quota_percent}%",
            f"--property=TasksMax={limits.tasks_max}",
            f"--property=RuntimeMaxSec={limits.timeout_seconds + 300}",
            f"--setenv=PYTHONPATH={pythonpath}",
            f"--setenv={ENV_PREFIX}_CODE_TASK_SYSTEMD_UNIT={unit}.service",
        ]
        for name in _worker_environment_names():
            value = os.environ.get(name)
            if value:
                command.append(f"--setenv={name}={value}")
        command.extend(worker)
        result = subprocess.run(
            command,
            cwd=str(source_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            raise RuntimeError(f"systemd-run failed: {detail}")
        worker_ref = f"{unit}.service"
        write_json_atomic(
            request_path.parent / DISPATCH_FILENAME,
            {
                "worker": worker_ref,
                "kind": "systemd",
                "boot_id": _boot_id(),
                "dispatched_at": time.time(),
                "attempt": attempt,
            },
        )
        return worker_ref

    env = {
        name: value
        for name in _worker_environment_names()
        if (value := os.environ.get(name))
    }
    env.update(
        {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": pythonpath,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
    )
    stdout_stream = (request_path.parent / "stdout.log").open("a", encoding="utf-8")
    stderr_stream = (request_path.parent / "stderr.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed module worker
            worker,
            cwd=str(source_root),
            env=env,
            stdout=stdout_stream,
            stderr=stderr_stream,
            text=True,
            start_new_session=True,
        )
    finally:
        stdout_stream.close()
        stderr_stream.close()
    worker_ref = f"process:{process.pid}"
    write_json_atomic(
        request_path.parent / DISPATCH_FILENAME,
        {
            "worker": worker_ref,
            "kind": "process",
            "pid": process.pid,
            "proc_start_ticks": _process_start_ticks(process.pid),
            "boot_id": _boot_id(),
            "dispatched_at": time.time(),
            "attempt": attempt,
        },
    )
    return worker_ref


def _dispatch_is_active(dispatch: Mapping[str, Any]) -> bool:
    if str(dispatch.get("boot_id") or "") != _boot_id():
        return False
    kind = str(dispatch.get("kind") or "")
    worker = str(dispatch.get("worker") or "")
    if kind == "systemd" and worker and shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", worker],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    if kind == "process":
        try:
            pid = int(dispatch.get("pid") or worker.removeprefix("process:"))
            os.kill(pid, 0)
        except (OSError, TypeError, ValueError):
            return False
        return pid > 1
    return False


def code_task_dispatch_active(job_dir: Path) -> bool:
    return _dispatch_is_active(read_json_file(job_dir / DISPATCH_FILENAME) or {})


def _boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _process_start_ticks(pid: int) -> int:
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text(
            encoding="ascii"
        ).split()
        return int(fields[21])
    except (OSError, IndexError, ValueError):
        return 0


def _worker_environment_names() -> tuple[str, ...]:
    return (
        f"{ENV_PREFIX}_SOURCE_ROOT",
        f"{ENV_PREFIX}_DEV_ROOT",
        f"{ENV_PREFIX}_DEV_ALLOWED_PATHS",
        f"{ENV_PREFIX}_DEV_DENIED_PATHS",
        f"{ENV_PREFIX}_LOG_DIR",
        f"{ENV_PREFIX}_INSTANCE_ID",
        f"{ENV_PREFIX}_CODEX_BIN",
        f"{ENV_PREFIX}_CODEX_BOT_HOME",
        f"{ENV_PREFIX}_CODE_MODEL",
        f"{ENV_PREFIX}_CODE_REASONING_EFFORT",
        f"{ENV_PREFIX}_CODE_TASK_GITHUB_REPOSITORY",
        f"{ENV_PREFIX}_CODE_TASK_GITHUB_TOKEN_FILE",
        f"{ENV_PREFIX}_CODE_TASK_GIT_AUTHOR_NAME",
        f"{ENV_PREFIX}_CODE_TASK_GIT_AUTHOR_EMAIL",
        f"{ENV_PREFIX}_CODE_TASK_QUICK_COMMAND",
        f"{ENV_PREFIX}_CODE_TASK_FULL_COMMAND",
        f"{ENV_PREFIX}_CODE_TASK_TIMEOUT_SECONDS",
        f"{ENV_PREFIX}_CODE_TASK_MEMORY_MAX_BYTES",
        f"{ENV_PREFIX}_CODE_TASK_CPU_QUOTA_PERCENT",
        f"{ENV_PREFIX}_CODE_TASK_TASKS_MAX",
        f"{ENV_PREFIX}_CODE_TASK_DISK_MAX_BYTES",
        f"{ENV_PREFIX}_CODE_TASK_HEARTBEAT_SECONDS",
        f"{ENV_PREFIX}_CODE_TASK_PROGRESS_NOTIFY_SECONDS",
        f"{ENV_PREFIX}_CODE_TASK_CANCEL_GRACE_SECONDS",
        f"{ENV_PREFIX}_LIMIT_DIR",
        f"{ENV_PREFIX}_JOB_POLL_INTERVAL",
    )


def _resource_sample(pid: int, job_dir: Path) -> dict[str, Any]:
    rss_bytes = 0
    status_path = Path("/proc") / str(pid) / "status"
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                try:
                    rss_bytes = int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    pass
                break
    return {
        "pid": pid,
        "rss_bytes": rss_bytes,
        "disk_bytes": _directory_size(job_dir),
    }


def _directory_size(root: Path) -> int:
    total = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in files:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total


def cleanup_code_task_retention(
    workspace_root: Path,
    *,
    retention_seconds: int = 7 * 24 * 60 * 60,
    total_max_bytes: int = 5 * 1024**3,
    now: float | None = None,
) -> dict[str, int]:
    """Purge retained task sandboxes and enforce the per-instance disk ceiling."""
    current = time.time() if now is None else now
    jobs = _code_task_job_dirs(workspace_root)
    purged = 0
    removed = 0
    for job_dir in jobs:
        status = read_json_file(job_dir / "status.json") or {}
        state = str(status.get("status") or "")
        updated = float(status.get("updated_at") or job_dir.stat().st_mtime)
        if state not in {"failed", "cancelled", "interrupted"}:
            continue
        if current - updated >= retention_seconds:
            purged += _purge_heavy_task_artifacts(job_dir)

    total = sum(_directory_size(job_dir) for job_dir in jobs if job_dir.exists())
    if total > total_max_bytes:
        terminal = []
        for job_dir in jobs:
            status = read_json_file(job_dir / "status.json") or {}
            state = str(status.get("status") or "")
            if state not in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }:
                continue
            terminal.append(
                (
                    float(status.get("updated_at") or job_dir.stat().st_mtime),
                    job_dir,
                )
            )
        for _, job_dir in sorted(terminal):
            if total <= total_max_bytes:
                break
            size = _directory_size(job_dir)
            shutil.rmtree(job_dir)
            total = max(0, total - size)
            removed += 1
    return {
        "purged_artifacts": purged,
        "removed_tasks": removed,
        "bytes_remaining": total,
    }


def _code_task_job_dirs(workspace_root: Path) -> list[Path]:
    jobs: list[Path] = []
    if not workspace_root.is_dir():
        return jobs
    for request_path in workspace_root.glob("**/jobs/job_*/request.json"):
        request = read_json_file(request_path) or {}
        if str(request.get("tool_name") or "") == "start_code_task":
            jobs.append(request_path.parent)
    return jobs


def _purge_heavy_task_artifacts(job_dir: Path) -> int:
    purged = 0
    for child in tuple(job_dir.iterdir()):
        if child.name in _HEAVY_RETAINED_NAMES:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
            purged += 1
            continue
        if child.name.startswith(("codex-", "validation-")) and child.suffix in {
            ".log",
            ".jsonl",
        }:
            child.unlink(missing_ok=True)
            purged += 1
    return purged


def _cleanup_success(paths: CodeTaskPaths) -> None:
    shutil.rmtree(paths.worktree, ignore_errors=True)
    shutil.rmtree(paths.task_home, ignore_errors=True)
    shutil.rmtree(paths.validation_home, ignore_errors=True)


def _source_root() -> Path:
    raw = (
        os.environ.get(f"{ENV_PREFIX}_SOURCE_ROOT", "").strip()
        or os.environ.get(f"{ENV_PREFIX}_DEV_ROOT", "").strip()
    )
    if not raw:
        raise ToolHandlerError(
            "source root is not configured",
            error_code="code_task_source_missing",
            stage="preparing",
        )
    root = Path(raw).expanduser().resolve()
    if not (root / ".git").exists():
        raise ToolHandlerError(
            f"source root is not a Git worktree: {root}",
            error_code="code_task_source_invalid",
            stage="preparing",
        )
    return root


def _codex_binary() -> Path:
    raw = os.environ.get(f"{ENV_PREFIX}_CODEX_BIN", "").strip()
    if not raw:
        raise ToolHandlerError(
            f"{ENV_PREFIX}_CODEX_BIN must name an absolute Codex executable",
            error_code="code_task_codex_bin_missing",
            stage="preparing",
        )
    binary = Path(raw).expanduser()
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ToolHandlerError(
            f"configured Codex binary is not executable: {binary}",
            error_code="code_task_codex_bin_invalid",
            stage="preparing",
        )
    return binary.resolve()


def _hash_path(path: Path) -> str:
    if path.is_symlink():
        target = os.readlink(path).encode("utf-8", errors="surrogateescape")
        return "symlink:" + hashlib.sha256(target).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_output(root: Path, args: list[str]) -> str:
    result = _run(["git", *args], cwd=root, timeout=60)
    return result.stdout.strip()


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def _tail_text(path: Path, limit: int = 2000) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


def _is_codex_auth_failure(detail: str) -> bool:
    normalized = str(detail or "").lower()
    return any(
        marker in normalized
        for marker in (
            "refresh token",
            "token has already been used",
            "token already used",
            "unauthorized",
            "authentication",
            "not logged in",
            "login required",
            "status code 401",
            "http 401",
        )
    )


def _codex_process_error(returncode: int, detail: str) -> ToolHandlerError:
    if _is_codex_auth_failure(detail):
        return ToolHandlerError(
            (
                "Codex worker authentication is unavailable; run "
                "`python -m chatcopilot bot codex-auth login "
                "--bot <bot.yaml> --lane worker`."
            ),
            error_code="code_task_codex_auth_unavailable",
            stage="running",
            details={"diagnostic": "codex-stderr.log"},
        )
    return ToolHandlerError(
        f"Codex exited with code {returncode}: {detail}",
        error_code="code_task_codex_failed",
        stage="running",
    )


def _set_stage(
    ctx: ToolContext,
    stage: str,
    message: str,
    *,
    details: Mapping[str, object] | None = None,
) -> None:
    ctx.job.update_stage(stage, message, details=details)


def _env_bool(name: str, fallback: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, fallback: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(fallback))))
    except ValueError:
        return fallback


__all__ = [
    "CHANGES_FILENAME",
    "CODEX_EVENTS_FILENAME",
    "CODEX_SESSION_FILENAME",
    "DISPATCH_FILENAME",
    "CodeTaskChange",
    "CodeTaskPaths",
    "build_bwrap_command",
    "cleanup_code_task_retention",
    "code_task_dispatch_active",
    "code_task_limits",
    "execute_code_task",
    "schedule_code_task_worker",
    "terminate_recorded_task",
]
