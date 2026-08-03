from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from chatcopilot.contracts.tools import ToolHandlerError
from chatcopilot.contracts.tools import ToolContext
from chatcopilot.contracts.code_tasks import validate_code_task_transition
from chatcopilot.core.jobs import (
    BackgroundJob,
    code_task_state_lock,
    request_job_cancel,
    write_job_status,
    write_json_atomic,
)
from chatcopilot.external_tools.dev import code_task_runtime as runtime
from chatcopilot.external_tools.dev import code_task_service
from chatcopilot.middleware.acp.job_dispatch import extract_code_task_command


_PUBLIC_TITLE = "修复代码任务回归"

def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "code-task@example.invalid")
    _git(source, "config", "user.name", "Code Task Test")
    (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (source / "tracked.txt").write_text("tracked baseline\n", encoding="utf-8")
    _git(source, "add", ".gitignore", "tracked.txt")
    _git(source, "commit", "-m", "baseline")
    return source


def _paths(tmp_path: Path, source: Path) -> runtime.CodeTaskPaths:
    job_dir = tmp_path / "job"
    job_dir.mkdir(mode=0o700)
    return runtime.CodeTaskPaths.build(job_dir=job_dir, source_root=source)


class _JobContext:
    def __init__(self, job_dir: Path) -> None:
        self.job_dir = job_dir
        self.job_id = job_dir.name

    def update_stage(
        self,
        stage: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        write_job_status(
            self.job_dir,
            stage,
            message,
            stage=stage,
            details=details,
        )


def _fake_codex(tmp_path: Path) -> Path:
    binary = tmp_path / "fake-codex"
    binary.write_text(
        """#!/usr/bin/python3
import json
import pathlib
import sys

prompt = sys.stdin.read()
root = pathlib.Path("/workspace")
(root / "tracked.txt").write_text("agent result\\n", encoding="utf-8")
(root / "codex-args.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
if "resume succeeds" in prompt:
    (root / "pass.marker").write_text("ok\\n", encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "thread-test-1"}), flush=True)
print(json.dumps({
    "type": "item.completed",
    "item": {
        "type": "agent_message",
        "text": "implemented " + json.dumps(sys.argv[1:]),
    },
}), flush=True)
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _configure_fake_task(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: Path,
    codex: Path,
    auth_home: Path,
    full_command: str = "true",
) -> None:
    auth_home.mkdir(mode=0o700)
    worker_home = auth_home / "worker"
    worker_home.mkdir(mode=0o700)
    (worker_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "id-fake",
                    "access_token": "access-fake",
                    "refresh_token": "refresh-fake",
                    "account_id": "test-account",
                },
                "last_refresh": "2026-07-28T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (worker_home / "auth.json").chmod(0o600)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(source))
    monkeypatch.setenv("CHATCOPILOT_CODEX_BIN", str(codex))
    monkeypatch.setenv("CHATCOPILOT_CODEX_BOT_HOME", str(auth_home))
    monkeypatch.setenv("CHATCOPILOT_CODE_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("CHATCOPILOT_CODE_REASONING_EFFORT", "max")
    monkeypatch.setenv("CHATCOPILOT_CODE_TASK_QUICK_COMMAND", "true")
    monkeypatch.setenv("CHATCOPILOT_CODE_TASK_FULL_COMMAND", full_command)
    monkeypatch.setenv("CHATCOPILOT_INSTANCE_ID", "test-instance")
    monkeypatch.setenv("CHATCOPILOT_DEV_ROOT", str(source))
    monkeypatch.setenv("CHATCOPILOT_DEV_ALLOWED_PATHS", "**")
    monkeypatch.setenv("CHATCOPILOT_CODE_TASK_HEARTBEAT_SECONDS", "5")

    def prepare_clone(
        *,
        job_dir: Path,
        worktree: Path,
        instance_id: str,
        source_root: Path,
    ) -> dict[str, object]:
        del job_dir
        assert instance_id == "test-instance"
        assert source_root == source
        if not worktree.exists():
            shutil.copytree(source, worktree)
        return {}

    monkeypatch.setattr(runtime, "prepare_delivery_worktree", prepare_clone)
    monkeypatch.setattr(
        runtime,
        "deliver_pull_request",
        lambda **_kwargs: {
            "delivered": True,
            "branch": "codex/test-instance/job",
            "commit_sha": "a" * 40,
            "pr_url": "https://github.example/pull/1",
            "pr_number": 1,
            "draft": True,
        },
    )


def _persist_code_job(
    workspace_root: Path,
    *,
    name: str,
    status: str,
    updated_at: float = 1.0,
    instance_id: str | None = "test-instance",
) -> Path:
    job_dir = workspace_root / "owner" / "jobs" / name
    job_dir.mkdir(parents=True)
    request = {
        "job_id": name,
        "tool_name": "start_code_task",
        "attempts": [{"number": 1, "status": status}],
    }
    if instance_id is not None:
        request["instance_id"] = instance_id
    write_json_atomic(job_dir / "request.json", request)
    write_json_atomic(
        job_dir / "status.json",
        {
            "status": status,
            "stage": status,
            "attempt": 1,
            "updated_at": updated_at,
        },
    )
    return job_dir


def test_worker_credential_root_is_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHATCOPILOT_CODEX_BOT_HOME", raising=False)
    with pytest.raises(ToolHandlerError, match="CODEX_BOT_HOME") as missing:
        runtime._worker_credential_root()
    assert missing.value.error_code == "code_task_dedicated_auth_missing"

    dedicated = tmp_path / "dedicated"
    monkeypatch.setenv("CHATCOPILOT_CODEX_BOT_HOME", str(dedicated))
    assert runtime._worker_credential_root() == dedicated

    monkeypatch.setenv("CODEX_HOME", str(dedicated))
    with pytest.raises(ToolHandlerError) as personal:
        runtime._worker_credential_root()
    assert personal.value.error_code == "code_task_personal_auth_forbidden"

    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setenv("CHATCOPILOT_CODEX_BOT_HOME", "relative/auth")
    with pytest.raises(ToolHandlerError) as relative:
        runtime._worker_credential_root()
    assert relative.value.error_code == "code_task_dedicated_auth_invalid"

    monkeypatch.setenv(
        "CHATCOPILOT_CODEX_BOT_HOME",
        str(Path("~").expanduser() / ".codex"),
    )
    with pytest.raises(ToolHandlerError) as default_personal:
        runtime._worker_credential_root()
    assert default_personal.value.error_code == "code_task_personal_auth_forbidden"

    monkeypatch.setenv(
        "CHATCOPILOT_CODEX_BOT_HOME",
        str(Path("~").expanduser() / ".codex" / "bot-auth"),
    )
    with pytest.raises(ToolHandlerError) as personal_descendant:
        runtime._worker_credential_root()
    assert (
        personal_descendant.value.error_code
        == "code_task_personal_auth_forbidden"
    )


def test_worker_credential_generation_invalidates_resume_and_uses_worker_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _repository(tmp_path)
    job_dir = tmp_path / "job-generation"
    job_dir.mkdir(mode=0o700)
    auth_root = tmp_path / "auth-root"
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(source))
    monkeypatch.setenv("CHATCOPILOT_CODEX_BOT_HOME", str(auth_root))
    monkeypatch.setenv("CHATCOPILOT_DEV_ROOT", str(source))
    monkeypatch.setenv("CHATCOPILOT_DEV_ALLOWED_PATHS", "**")
    write_json_atomic(
        job_dir / runtime.CODEX_SESSION_FILENAME,
        {
            "native_session_id": "thread-old-account",
            "credential_generation": 6,
            "updated_at": 1.0,
        },
    )

    lease_events: list[tuple[str, Path, str, Path]] = []

    class _Lease:
        generation = 7

    @contextmanager
    def fake_lease(
        root: Path,
        lane: str,
        runtime_home: Path,
    ) -> Iterator[_Lease]:
        lease_events.append(("enter", root, lane, runtime_home))
        try:
            yield _Lease()
        finally:
            lease_events.append(("exit", root, lane, runtime_home))

    invocation: dict[str, object] = {}

    def fake_run(
        paths: runtime.CodeTaskPaths,
        prompt: str,
        *,
        native_session_id: str,
        limits: object,
    ) -> tuple[str, str]:
        del paths, limits
        invocation["prompt"] = prompt
        invocation["native_session_id"] = native_session_id
        return "worker completed", "thread-new-account"

    monkeypatch.setattr(runtime, "credential_lease", fake_lease)
    monkeypatch.setattr(runtime, "_prepare_task", lambda _paths: None)
    monkeypatch.setattr(runtime, "_run_codex_stream", fake_run)
    monkeypatch.setattr(runtime, "_task_changes", lambda _paths: ())
    monkeypatch.setattr(
        runtime,
        "_validate_task",
        lambda _paths, _changes, *, limits: [],
    )
    monkeypatch.setattr(runtime, "_cleanup_success", lambda _paths: None)
    ctx = ToolContext(job=_JobContext(job_dir))

    runtime.execute_code_task(
        {"prompt": "continue safely", "title": _PUBLIC_TITLE},
        ctx,
    )

    expected_runtime_home = job_dir / "task-home" / ".codex"
    assert lease_events == [
        ("enter", auth_root, "worker", expected_runtime_home),
        ("exit", auth_root, "worker", expected_runtime_home),
    ]
    assert invocation["native_session_id"] == ""
    assert str(invocation["prompt"]).startswith(
        "Implement this task in the isolated repository worktree."
    )
    session = json.loads(
        (job_dir / runtime.CODEX_SESSION_FILENAME).read_text(encoding="utf-8")
    )
    assert session["native_session_id"] == "thread-new-account"
    assert session["credential_generation"] == 7


def test_worker_credential_lease_exits_when_codex_invocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _repository(tmp_path)
    job_dir = tmp_path / "job-invocation-failure"
    job_dir.mkdir(mode=0o700)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(source))
    monkeypatch.setenv("CHATCOPILOT_CODEX_BOT_HOME", str(tmp_path / "auth-root"))
    exited = False

    class _Lease:
        generation = 3

    @contextmanager
    def fake_lease(
        _root: Path,
        lane: str,
        _runtime_home: Path,
    ) -> Iterator[_Lease]:
        nonlocal exited
        assert lane == "worker"
        try:
            yield _Lease()
        finally:
            exited = True

    def fail_run(
        _paths: runtime.CodeTaskPaths,
        _prompt: str,
        *,
        native_session_id: str,
        limits: object,
    ) -> tuple[str, str]:
        del native_session_id, limits
        raise ToolHandlerError(
            "Codex stopped",
            error_code="code_task_codex_failed",
            stage="running",
        )

    monkeypatch.setattr(runtime, "credential_lease", fake_lease)
    monkeypatch.setattr(runtime, "_prepare_task", lambda _paths: None)
    monkeypatch.setattr(runtime, "_run_codex_stream", fail_run)
    ctx = ToolContext(job=_JobContext(job_dir))

    with pytest.raises(ToolHandlerError) as failed:
        runtime.execute_code_task(
            {"prompt": "trigger failure", "title": _PUBLIC_TITLE},
            ctx,
        )

    assert failed.value.error_code == "code_task_codex_failed"
    assert exited is True


def test_worker_auth_error_detection_does_not_expose_raw_stderr() -> None:
    raw_error = (
        "401 Unauthorized: refresh token already used; "
        "secret-token-must-stay-private"
    )

    error = runtime._codex_process_error(1, raw_error)

    assert error.error_code == "code_task_codex_auth_unavailable"
    assert "codex-auth login" in str(error)
    assert "secret-token" not in str(error)
    assert error.details == {"diagnostic": "codex-stderr.log"}
    assert not runtime._is_codex_auth_failure("repository validation failed")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
def test_fake_codex_jsonl_task_streams_validates_and_keeps_source_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _repository(tmp_path)
    job_dir = tmp_path / "job-success"
    job_dir.mkdir(mode=0o700)
    _configure_fake_task(
        monkeypatch,
        source=source,
        codex=_fake_codex(tmp_path),
        auth_home=tmp_path / "dedicated",
    )
    ctx = ToolContext(job=_JobContext(job_dir))

    summary, outputs, _ = runtime.execute_code_task(
        {
            "prompt": "implement a regression fix",
            "title": _PUBLIC_TITLE,
            "acceptance_criteria": ["tests pass"],
        },
        ctx,
    )

    payload = json.loads(summary)
    assert payload["changed_files"] == ["codex-args.json", "tracked.txt"]
    assert payload["delivered"] is True
    assert payload["draft"] is True
    assert "--ignore-user-config" in payload["final"]
    assert "--ignore-rules" in payload["final"]
    assert "mcp_servers={}" in payload["final"]
    assert "features.hooks=false" in payload["final"]
    assert outputs == [str(job_dir)]
    assert (source / "tracked.txt").read_text(encoding="utf-8") == "tracked baseline\n"
    assert not (job_dir / "worktree").exists()
    assert "thread-test-1" in (job_dir / runtime.CODEX_EVENTS_FILENAME).read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
def test_failed_task_retains_worktree_and_resume_reuses_native_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _repository(tmp_path)
    job_dir = tmp_path / "job-resume"
    job_dir.mkdir(mode=0o700)
    _configure_fake_task(
        monkeypatch,
        source=source,
        codex=_fake_codex(tmp_path),
        auth_home=tmp_path / "dedicated",
        full_command="test -f /workspace/pass.marker",
    )
    ctx = ToolContext(job=_JobContext(job_dir))

    with pytest.raises(ToolHandlerError) as failed:
        runtime.execute_code_task(
            {"prompt": "first attempt fails", "title": _PUBLIC_TITLE},
            ctx,
        )
    assert failed.value.error_code == "code_task_validation_failed"
    assert (job_dir / "worktree").is_dir()
    session = json.loads(
        (job_dir / runtime.CODEX_SESSION_FILENAME).read_text(encoding="utf-8")
    )
    assert session["native_session_id"] == "thread-test-1"
    assert session["credential_generation"] == 0

    summary, _, _ = runtime.execute_code_task(
        {
            "prompt": "resume succeeds",
            "title": _PUBLIC_TITLE,
            "acceptance_criteria": [],
        },
        ctx,
    )

    payload = json.loads(summary)
    assert payload["changed_files"] == [
        "codex-args.json",
        "pass.marker",
        "tracked.txt",
    ]
    assert "resume" in payload["final"]
    assert "thread-test-1" in payload["final"]
    resume_args = json.loads(payload["final"].removeprefix("implemented "))
    resume_index = resume_args.index("resume")
    assert resume_args[resume_index:] == ["resume", "thread-test-1", "-"]
    for option in (
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "-m",
        "-C",
    ):
        assert resume_args.index(option) < resume_index
    assert "--search" not in resume_args
    assert 'web_search="live"' in resume_args
    events = [
        json.loads(line)
        for line in (job_dir / runtime.CODEX_EVENTS_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert sum(event.get("type") == "thread.started" for event in events) == 2
    assert not (job_dir / "worktree").exists()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
def test_bwrap_probe_exposes_only_worktree_and_clears_host_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _repository(tmp_path)
    paths = _paths(tmp_path, source)
    shutil.copytree(source, paths.worktree)
    secret = "platform-secret-must-not-cross-boundary"
    monkeypatch.setenv("FEISHU_APP_SECRET", secret)
    source_file = source / "tracked.txt"
    probe = " && ".join(
        [
            "test -w /workspace",
            "test -r /workspace/tracked.txt",
            f"test ! -e {source_file}",
            'test "$HOME" = /sandbox-home/agent',
            'test ! -e "$HOME/.ssh"',
            "test ! -S /run/docker.sock",
            'test -z "${FEISHU_APP_SECRET:-}"',
            "git status --short >/dev/null",
            "readlink /proc/self/ns/net",
        ]
    )
    command = runtime.build_bwrap_command(
        paths,
        ["/bin/bash", "-lc", probe],
        include_codex=False,
    )

    assert "--share-net" in command
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == os.readlink("/proc/self/ns/net")


def test_recovery_dispatches_queue_and_interrupts_stale_running_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATCOPILOT_INSTANCE_ID", "test-instance")
    queued = _persist_code_job(tmp_path, name="job_queued", status="queued")
    running = _persist_code_job(tmp_path, name="job_running", status="running")
    dispatched: list[Path] = []
    monkeypatch.setattr(runtime, "code_task_dispatch_active", lambda _path: False)
    monkeypatch.setattr(
        runtime,
        "schedule_code_task_worker",
        lambda request: dispatched.append(request) or "unit.service",
    )

    counts = code_task_service.recover_code_tasks_once(tmp_path)

    assert dispatched == [queued / "request.json"]
    assert counts["dispatched"] == 1
    assert counts["interrupted"] == 1
    interrupted = json.loads((running / "status.json").read_text(encoding="utf-8"))
    assert interrupted["status"] == "interrupted"
    result = json.loads((running / "result.json").read_text(encoding="utf-8"))
    assert result["error_code"] == "code_task_interrupted"
    request = json.loads((running / "request.json").read_text(encoding="utf-8"))
    assert request["attempts"][0]["status"] == "interrupted"



def test_recovery_skips_foreign_and_unversioned_instance_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATCOPILOT_INSTANCE_ID", "test-instance")
    own = _persist_code_job(tmp_path, name="job_own", status="queued")
    foreign = _persist_code_job(
        tmp_path,
        name="job_foreign",
        status="queued",
        instance_id="other-instance",
    )
    missing = _persist_code_job(
        tmp_path,
        name="job_missing",
        status="queued",
        instance_id=None,
    )
    dispatched: list[Path] = []
    monkeypatch.setattr(runtime, "code_task_dispatch_active", lambda _path: False)
    monkeypatch.setattr(
        runtime,
        "schedule_code_task_worker",
        lambda request: dispatched.append(request) or "unit.service",
    )

    counts = code_task_service.recover_code_tasks_once(tmp_path)

    assert dispatched == [own / "request.json"]
    assert counts["scanned"] == 3
    assert counts["skipped_instance"] == 2
    assert counts["dispatched"] == 1
    for skipped in (foreign, missing):
        status = json.loads((skipped / "status.json").read_text(encoding="utf-8"))
        assert status["status"] == "queued"
        assert not (skipped / "result.json").exists()


def test_recovery_requires_current_instance_without_touching_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = _persist_code_job(tmp_path, name="job_owned", status="queued")
    before_status = (job_dir / "status.json").read_bytes()
    monkeypatch.delenv("CHATCOPILOT_INSTANCE_ID", raising=False)

    with pytest.raises(RuntimeError, match="INSTANCE_ID"):
        code_task_service.recover_code_tasks_once(tmp_path)

    assert (job_dir / "status.json").read_bytes() == before_status
    assert not (job_dir / "result.json").exists()


def test_delivery_transition_serializes_against_cancellation(
    tmp_path: Path,
) -> None:
    job_dir = _persist_code_job(
        tmp_path,
        name="job_cancel_race",
        status="validating",
    )
    job = BackgroundJob(
        job_id=job_dir.name,
        tool_name="start_code_task",
        execution_policy="global_serial_background",
        job_dir=job_dir,
        request_path=job_dir / "request.json",
        result_path=job_dir / "result.json",
    )
    started = threading.Event()
    finished = threading.Event()
    outcomes: list[bool] = []
    failures: list[BaseException] = []

    def cancel() -> None:
        started.set()
        try:
            outcomes.append(request_job_cancel(job, requested_by="owner"))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=cancel)
    with code_task_state_lock(job_dir):
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
        write_job_status(
            job_dir,
            "delivering",
            "Opening draft PR.",
            stage="delivering",
        )
    thread.join(timeout=2)

    assert not failures
    assert outcomes == [False]
    assert not job.cancellation_path.exists()
    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "delivering"


def test_generic_job_cancellation_does_not_use_code_task_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "generic-job"
    job_dir.mkdir()
    write_job_status(job_dir, "running", "Running.")
    job = BackgroundJob(
        job_id=job_dir.name,
        tool_name="generic_background_tool",
        execution_policy="background",
        job_dir=job_dir,
        request_path=job_dir / "request.json",
        result_path=job_dir / "result.json",
    )

    def forbidden_lock(_job_dir: Path) -> None:
        raise AssertionError("generic jobs must not use the POSIX code-task lock")

    monkeypatch.setattr(
        "chatcopilot.core.jobs.code_task_state_lock",
        forbidden_lock,
    )

    assert request_job_cancel(job, requested_by="owner") is True
    assert job.cancellation_path.is_file()


def test_retention_purges_old_failure_artifacts_and_never_active_task(
    tmp_path: Path,
) -> None:
    failed = _persist_code_job(tmp_path, name="job_failed", status="failed")
    active = _persist_code_job(tmp_path, name="job_active", status="running")
    for job in (failed, active):
        (job / "worktree").mkdir()
        (job / "worktree" / "large.bin").write_bytes(b"x" * 128)
        (job / "codex-events.jsonl").write_text("{}\n", encoding="utf-8")

    result = runtime.cleanup_code_task_retention(
        tmp_path,
        retention_seconds=10,
        total_max_bytes=1024**2,
        now=100.0,
    )

    assert result["purged_artifacts"] == 2
    assert not (failed / "worktree").exists()
    assert not (failed / "codex-events.jsonl").exists()
    assert (active / "worktree").is_dir()


def test_recorded_process_termination_checks_boot_and_process_start_time(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job-cancel"
    job_dir.mkdir()
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        supervisor = {
            "pid": process.pid,
            "pgid": os.getpgid(process.pid),
            "boot_id": runtime._boot_id(),
            "proc_start_ticks": runtime._process_start_ticks(process.pid),
        }
        write_json_atomic(job_dir / runtime.SUPERVISOR_FILENAME, supervisor)
        supervisor["proc_start_ticks"] += 1
        write_json_atomic(job_dir / runtime.SUPERVISOR_FILENAME, supervisor)

        assert runtime.terminate_recorded_task(job_dir, grace_seconds=1) is False
        assert process.poll() is None

        supervisor["proc_start_ticks"] -= 1
        write_json_atomic(job_dir / runtime.SUPERVISOR_FILENAME, supervisor)
        assert runtime.terminate_recorded_task(job_dir, grace_seconds=1) is True
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/task", ("task", "")),
        (" /TASK job_20260724_010203_deadbeef ", ("task", "job_20260724_010203_deadbeef")),
        ("/cancel", ("cancel", "")),
        ("/cancel job_20260724_010203_deadbeef", ("cancel", "job_20260724_010203_deadbeef")),
        ("请 /cancel", None),
        ("/cancel invalid", None),
    ],
)
def test_code_task_commands_are_exact_and_deterministic(
    text: str,
    expected: tuple[str, str] | None,
) -> None:
    assert extract_code_task_command(text) == expected


def test_code_task_state_machine_rejects_terminal_or_skipped_transitions() -> None:
    validate_code_task_transition("queued", "preparing")
    validate_code_task_transition("failed", "queued")
    validate_code_task_transition("running", "running")
    with pytest.raises(ValueError, match="queued -> succeeded"):
        validate_code_task_transition("queued", "succeeded")
    with pytest.raises(ValueError, match="succeeded -> queued"):
        validate_code_task_transition("succeeded", "queued")
