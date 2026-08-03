from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from console.control import diagnostics
from console.control.instances import BotInstance
from chatcopilot.middleware.runtime.workspace.cleanup import cleanup_diagnostic_records
from chatcopilot.core.log_context import bind_log_context, current_log_context
from chatcopilot.external_tools.shared.tool_spec import EXECUTION_USER_SERIAL_BACKGROUND
from chatcopilot.middleware.runtime.jobs import submitter as job_submitter
from chatcopilot.middleware.runtime.workspace import Workspace


TASK_ID = "task_20260623_120000_1234abcd"
JOB_ID = "job_20260623_120100_deadbeef"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _instance(root: Path, logs: Path) -> BotInstance:
    return BotInstance(
        instance_id="test-bot",
        bot_spec="bots/test/bot.yaml",
        workspace_root=str(root),
        log_dir=str(logs),
    )


def _seed(root: Path, logs: Path) -> tuple[Path, Path]:
    now = time.time()
    task = root / "p2p_user" / "tasks" / TASK_ID
    job = root / "p2p_user" / "jobs" / JOB_ID
    _write_json(task / "task.json", {
        "task_id": TASK_ID,
        "status": "failed",
        "progress": "tool failed",
        "session_id": "sid-1",
        "job_ids": [JOB_ID],
        "asked_at": now - 10,
        "finished_at": now,
    })
    _write_json(task / "turn.json", {
        "user_text": "debug this",
        "final_text": "failed",
        "api_key": "secret-value",
    })
    (task / "events.jsonl").write_text(
        json.dumps({"event": "turn_error", "data": {"message": "Authorization: Bearer abcdefgh"}}) + "\n",
        encoding="utf-8",
    )
    _write_json(
        job / "request.json",
        {
            "job_id": JOB_ID,
            "task_id": TASK_ID,
            "trace_id": TASK_ID,
            "prompt": "private source request",
            "args": {"path": "/private/repository", "value": "tool-private"},
            "user_id": "123456789",
            "chat_id": "987654321",
        },
    )
    _write_json(job / "status.json", {"status": "failed", "message": "token=job-secret-value", "updated_at": now})
    _write_json(job / "result.json", {"ok": False, "error": "boom", "traceback": "stack"})
    (job / "stdout.log").write_text("progress\n", encoding="utf-8")
    (job / "stderr.log").write_text("error\n", encoding="utf-8")
    day = time.strftime("%Y-%m-%d")
    runtime = logs / "runtime" / f"{day}.log"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(f"[{day} 12:00:00] ERROR x | task_id={TASK_ID} boom\n", encoding="utf-8")
    return task, job


def test_collect_task_bundle_links_job_and_redacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, logs = tmp_path / "workspaces", tmp_path / "logs"
    _seed(root, logs)
    monkeypatch.setattr(diagnostics, "discover_instances", lambda: [_instance(root, logs)])
    monkeypatch.setattr(diagnostics.operations, "status", lambda _instance: {"running": True})
    output = tmp_path / "bundle"

    result = diagnostics.collect_diagnostic_bundle(TASK_ID, output)

    assert result["instance_id"] == "test-bot"
    index = json.loads((output / "index.json").read_text(encoding="utf-8"))
    assert index["correlation"]["job_ids"] == [JOB_ID]
    assert (output / "jobs" / JOB_ID / "stderr.log").is_file()
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file())
    assert "secret-value" not in all_text
    assert "abcdefgh" not in all_text
    assert "job-secret-value" not in all_text
    assert "private source request" not in all_text
    assert "tool-private" not in all_text
    assert "123456789" not in all_text
    assert "987654321" not in all_text
    assert "[REDACTED]" in all_text


def test_large_job_stream_is_truncated_and_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, logs = tmp_path / "workspaces", tmp_path / "logs"
    _task, job = _seed(root, logs)
    (job / "stderr.log").write_text("x" * (diagnostics.MAX_JOB_STREAM_BYTES + 100), encoding="utf-8")
    monkeypatch.setattr(diagnostics, "discover_instances", lambda: [_instance(root, logs)])
    monkeypatch.setattr(diagnostics.operations, "status", lambda _instance: {})

    diagnostics.collect_diagnostic_bundle(JOB_ID, tmp_path / "bundle")

    index = json.loads((tmp_path / "bundle" / "index.json").read_text(encoding="utf-8"))
    assert f"jobs/{JOB_ID}/stderr.log" in index["truncated_files"]
    assert "[TRUNCATED TO TAIL]" in (tmp_path / "bundle" / "jobs" / JOB_ID / "stderr.log").read_text(encoding="utf-8")


def test_collect_job_bundle_finds_source_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, logs = tmp_path / "workspaces", tmp_path / "logs"
    _seed(root, logs)
    monkeypatch.setattr(diagnostics, "discover_instances", lambda: [_instance(root, logs)])
    monkeypatch.setattr(diagnostics.operations, "status", lambda _instance: {})

    diagnostics.collect_diagnostic_bundle(JOB_ID, tmp_path / "bundle")
    index = json.loads((tmp_path / "bundle" / "index.json").read_text(encoding="utf-8"))
    assert index["correlation"]["task_id"] == TASK_ID


def test_duplicate_id_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instances = []
    for name in ("one", "two"):
        root = tmp_path / name
        _seed(root, tmp_path / f"{name}-logs")
        instance = _instance(root, tmp_path / f"{name}-logs")
        instance.instance_id = name
        instances.append(instance)
    monkeypatch.setattr(diagnostics, "discover_instances", lambda: instances)
    with pytest.raises(diagnostics.DiagnosticError, match="多个位置"):
        diagnostics.collect_diagnostic_bundle(TASK_ID, tmp_path / "bundle")


def test_missing_id_lists_searched_instances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "discover_instances", lambda: [_instance(tmp_path, tmp_path / "logs")])
    with pytest.raises(diagnostics.DiagnosticError, match="已搜索实例: test-bot"):
        diagnostics.collect_diagnostic_bundle(TASK_ID, tmp_path / "bundle")


def test_cleanup_diagnostics_preserves_running_records(tmp_path: Path) -> None:
    old = time.time() - 40 * 86400
    finished = tmp_path / "p2p" / "tasks" / TASK_ID
    running = tmp_path / "p2p" / "jobs" / JOB_ID
    _write_json(finished / "task.json", {"status": "succeeded"})
    _write_json(running / "status.json", {"status": "running"})
    os.utime(finished / "task.json", (old, old))
    os.utime(finished, (old, old))
    os.utime(running / "status.json", (old, old))
    os.utime(running, (old, old))

    result = cleanup_diagnostic_records(tmp_path, retention_days=30, max_total_bytes=1024 * 1024)

    assert result["deleted_records"] == 1
    assert not finished.exists()
    assert running.exists()


def test_log_context_is_nested_and_restored() -> None:
    assert current_log_context() == {}
    with bind_log_context(task_id=TASK_ID, session_id="sid-1"):
        assert current_log_context()["task_id"] == TASK_ID
        with bind_log_context(job_id=JOB_ID):
            assert current_log_context()["job_id"] == JOB_ID
        assert "job_id" not in current_log_context()
    assert current_log_context() == {}



def test_code_task_submitter_requires_and_persists_instance_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(
        root=tmp_path / "workspace",
        user_id="owner",
        chat_kind="p2p",
        chat_id=None,
    ).ensure()
    monkeypatch.setattr(job_submitter, "_spawn_worker", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("CHATCOPILOT_INSTANCE_ID", raising=False)
    args = {
        "prompt": "implement the isolated change",
        "title": "修复实例隔离",
    }

    with pytest.raises(ValueError, match="INSTANCE_ID"):
        job_submitter.submit_tool_job(
            tool_name="start_code_task",
            args=args,
            execution_policy=EXECUTION_USER_SERIAL_BACKGROUND,
            workspace=workspace,
        )
    assert not (workspace.root / "jobs").exists()

    monkeypatch.setenv("CHATCOPILOT_INSTANCE_ID", "test-instance")
    job = job_submitter.submit_tool_job(
        tool_name="start_code_task",
        args=args,
        execution_policy=EXECUTION_USER_SERIAL_BACKGROUND,
        workspace=workspace,
    )
    request = json.loads(job.request_path.read_text(encoding="utf-8"))
    assert request["instance_id"] == "test-instance"

def test_background_request_persists_source_task_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = Workspace(
        root=tmp_path / "workspace",
        user_id="user",
        chat_kind="p2p",
        chat_id=None,
    ).ensure()
    monkeypatch.setattr(job_submitter, "_spawn_worker", lambda *_args, **_kwargs: None)

    job = job_submitter.submit_tool_job(
        tool_name="example",
        args={"value": 1},
        execution_policy=EXECUTION_USER_SERIAL_BACKGROUND,
        workspace=workspace,
        session_id="sid-1",
        trace_id=TASK_ID,
    )

    request = json.loads(job.request_path.read_text(encoding="utf-8"))
    assert request["trace_id"] == TASK_ID
    assert request["task_id"] == TASK_ID
