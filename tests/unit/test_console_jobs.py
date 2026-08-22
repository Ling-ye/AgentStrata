import hashlib
import json
import math
import os
from pathlib import Path

import pytest
from fastapi import HTTPException, Response

from console.backend.routes import bots as bot_routes
from console.control import observability, operations
from console.control.instances import BotInstance
from chatcopilot.contracts.code_tasks import CODE_TASK_ACTIVE_STATUSES


def _inst(workspace_root: Path) -> BotInstance:
    return BotInstance(
        instance_id="sample-bot",
        bot_spec="bots/sample-bot/bot.yaml",
        display_name="SampleBot",
        platform="feishu",
        workspace_root=str(workspace_root),
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _secure_task_record(task_dir: Path) -> None:
    task_dir.parent.chmod(0o700)
    task_dir.chmod(0o700)
    for path in task_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o600)


def test_observability_numeric_coercion_rejects_non_finite_values() -> None:
    assert observability._coerce_epoch(float("nan")) is None
    assert observability._coerce_epoch(float("inf")) is None
    assert observability._coerce_non_negative_int(float("inf")) == 0
    assert observability._coerce_non_negative_int("Infinity") == 0
    assert observability._coerce_non_negative_int("9" * 5000) == 0
    assert observability._coerce_epoch(10**4000) is None
    assert observability._timing_breakdown(
        [{"type": "llm", "elapsed_s": 10**4000}]
    )["model_s"] == 0.0
    saturated_timing = observability._timing_breakdown(
        [
            {"type": "llm", "elapsed_s": 1e308},
            {"type": "llm", "elapsed_s": 1e308},
        ]
    )
    assert math.isfinite(saturated_timing["model_s"])
    json.dumps(saturated_timing, allow_nan=False)
    cost = observability._actual_cost(
        [
            {
                "model": "deepseek-chat",
                "usage": {
                    "prompt_tokens": "9" * 5000,
                    "completion_tokens": -10,
                },
            }
        ]
    )
    assert cost["estimated_rmb"] == 0.0


def test_console_projects_context_snapshot_summary_limits() -> None:
    limits = observability._summary_limits(
        {
            "summary_limits": {
                "context_snapshots_total": 5001,
                "context_snapshots_retained": 5000,
                "context_snapshots_truncated": True,
                "context_snapshots_minimal": True,
                "llm_calls_total": 1001,
                "llm_calls_retained": 1000,
                "llm_calls_truncated": True,
                "input_resources_total": 501,
                "input_resources_retained": 500,
                "input_resources_truncated": True,
                "payload_truncated": True,
                "truncated": True,
            }
        }
    )

    assert limits["context_snapshots_total"] == 5001
    assert limits["context_snapshots_retained"] == 5000
    assert limits["context_snapshots_truncated"] is True
    assert limits["context_snapshots_minimal"] is True
    assert limits["llm_calls_total"] == 1001
    assert limits["llm_calls_retained"] == 1000
    assert limits["llm_calls_truncated"] is True
    assert limits["input_resources_total"] == 501
    assert limits["input_resources_retained"] == 500
    assert limits["input_resources_truncated"] is True
    assert limits["payload_truncated"] is True


def test_console_rejects_oversized_job_json_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "job" / "status.json"
    path.parent.mkdir()
    path.write_bytes(b'{' + b'"value":"' + b"x" * (8 * 1024 * 1024) + b'"}')

    assert observability._read_job_json(path) == {}


def test_console_rejects_near_limit_shallow_wide_job_json(tmp_path: Path) -> None:
    path = tmp_path / "job" / "status.json"
    path.parent.mkdir()
    raw = (
        b" " * (7 * 1024 * 1024)
        + b'{"items":['
        + b",".join([b"0"] * 200_010)
        + b"]}"
    )
    path.write_bytes(raw)

    assert observability._read_job_json(path) == {}


def test_console_rejects_group_or_other_writable_job_json(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    path = tmp_path / "task" / "task.json"
    _write_json(path, {"status": "trusted"})

    path.chmod(0o644)
    assert observability._read_job_json(path)["status"] == "trusted"

    path.chmod(0o666)
    assert observability._read_job_json(path) == {}


def _make_job(
    root: Path,
    rel_dir: str,
    job_id: str,
    *,
    status: str,
    message: str = "",
    updated_at: float | None = None,
    submitted_at: float | None = None,
    started_at: float | None = None,
    finished_at: float | None = None,
    tool_name: str = "external_diff",
    user_name: str = "Alice",
    stdout: str = "",
) -> Path:
    job_dir = root / rel_dir / "jobs" / job_id
    status_payload: dict[str, object] = {"status": status, "message": message}
    if updated_at is not None:
        status_payload["updated_at"] = updated_at
    _write_json(job_dir / "status.json", status_payload)

    request_payload: dict[str, object] = {
        "job_id": job_id,
        "tool_name": tool_name,
        "workspace": {"user_name": user_name, "user_id": "ou_test"},
    }
    if submitted_at is not None:
        request_payload["submitted_at"] = submitted_at
    _write_json(job_dir / "request.json", request_payload)

    result_payload: dict[str, object] = {"job_id": job_id, "tool_name": tool_name}
    if started_at is not None:
        result_payload["started_at"] = started_at
    if finished_at is not None:
        result_payload["finished_at"] = finished_at
    _write_json(job_dir / "result.json", result_payload)

    if stdout:
        (job_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    return job_dir


def test_jobs_scan_p2p_and_group_workspaces_sorted_by_latest_update(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    _make_job(
        root,
        "p2p_alice",
        "job_20260605_100000_old",
        status="queued",
        message="queued",
        updated_at=100,
        submitted_at=90,
    )
    _make_job(
        root,
        "group_oc_room/user_bob",
        "job_20260605_110000_new",
        status="succeeded",
        message="done",
        updated_at=200,
        submitted_at=80,
        started_at=120,
        finished_at=180,
        user_name="Bob",
    )

    resp = operations.jobs(_inst(root))

    assert resp["workspace_exists"] is True
    assert resp["count"] == 2
    ids = [job["job_id"] for job in resp["jobs"]]
    assert ids == ["job_20260605_110000_new", "job_20260605_100000_old"]
    assert resp["jobs"][0]["status"] == "succeeded"
    assert resp["jobs"][0]["submitter"] == "Bob"
    assert resp["jobs"][0]["elapsed_s"] == 60


def test_jobs_fallback_sort_statuses_and_stdout_tail(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    _make_job(
        root,
        "p2p_alice",
        "job_20260605_100000_failed",
        status="failed",
        message="failed",
        submitted_at=100,
        finished_at=300,
    )
    _make_job(
        root,
        "p2p_alice",
        "job_20260605_110000_running",
        status="running",
        message="running",
        submitted_at=50,
        started_at=400,
        stdout="\n".join(f"line-{idx}" for idx in range(25)),
    )

    resp = operations.jobs(_inst(root))

    assert [job["status"] for job in resp["jobs"]] == ["running", "failed"]
    running = resp["jobs"][0]
    assert running["sort_time"] == 400
    assert "line-24" in running["progress_tail"]
    assert "line-0" not in running["progress_tail"]
    assert running["progress_tail_integrity_gap"] is False


def test_jobs_reject_unsafe_stdout_tail_and_report_integrity_gap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    job_dir = _make_job(
        root,
        "p2p_alice",
        "job_20260605_111000_unsafe",
        status="running",
        stdout="safe initial output",
    )
    stdout = job_dir / "stdout.log"
    private = tmp_path / "private.txt"
    private.write_text("UNREDACTED_PRIVATE_CONTENT", encoding="utf-8")
    stdout.unlink()
    stdout.symlink_to(private)

    response = operations.jobs(_inst(root))

    assert response["jobs"][0]["progress_tail"] == ""
    assert response["jobs"][0]["stdout_age_s"] is None
    assert response["jobs"][0]["progress_tail_integrity_gap"] is True
    assert response["integrity_gap"] is True
    assert "UNREDACTED_PRIVATE_CONTENT" not in json.dumps(response)

    stdout.unlink()
    stdout.write_text("group writable output", encoding="utf-8")
    stdout.chmod(0o666)
    response = operations.jobs(_inst(root))
    assert response["jobs"][0]["progress_tail"] == ""
    assert response["jobs"][0]["progress_tail_integrity_gap"] is True


def test_jobs_reject_symlinked_job_directory_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    jobs_root = root / "p2p_alice" / "jobs"
    jobs_root.mkdir(parents=True)
    external = tmp_path / "external-private-job"
    _write_json(
        external / "status.json",
        {"status": "running", "message": "PRIVATE_STATUS_CONTENT"},
    )
    _write_json(
        external / "request.json",
        {"tool_name": "PRIVATE_TOOL_CONTENT"},
    )
    (external / "stdout.log").write_text(
        "PRIVATE_STDOUT_CONTENT",
        encoding="utf-8",
    )
    (jobs_root / "job_20260605_111500_ancestor").symlink_to(
        external,
        target_is_directory=True,
    )

    response = operations.jobs(_inst(root))
    serialized = json.dumps(response)

    assert "PRIVATE_STATUS_CONTENT" not in serialized
    assert "PRIVATE_TOOL_CONTENT" not in serialized
    assert "PRIVATE_STDOUT_CONTENT" not in serialized
    if response["jobs"]:
        assert response["jobs"][0]["progress_tail"] == ""
        assert response["jobs"][0]["progress_tail_integrity_gap"] is True


def test_jobs_reads_only_bounded_tail_from_large_stdout(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    job_dir = _make_job(
        root,
        "p2p_alice",
        "job_20260605_112000_large",
        status="running",
    )
    stdout = job_dir / "stdout.log"
    stdout.write_bytes((b"old-line\n" * (1024 * 1024)) + b"final-visible-line\n")

    response = operations.jobs(_inst(root))
    job = response["jobs"][0]

    assert "final-visible-line" in job["progress_tail"]
    assert len(job["progress_tail"]) <= 4000
    assert job["progress_tail_integrity_gap"] is False
    assert response["integrity_gap"] is False


def test_jobs_include_corrupt_json_without_crashing(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    job_dir = root / "p2p_alice" / "jobs" / "job_20260605_120000_corrupt"
    job_dir.mkdir(parents=True)
    (job_dir / "status.json").write_text("{bad json", encoding="utf-8")
    _write_json(
        job_dir / "request.json",
        {"submitted_at": 500, "workspace": {"user_id": "ou_alice"}},
    )

    resp = operations.jobs(_inst(root))

    assert resp["count"] == 1
    job = resp["jobs"][0]
    assert job["status"] == "unknown"
    assert job["submitter"] == "ou_alice"
    assert job["sort_time"] == 500


def test_jobs_tolerate_json_integer_over_parser_limit(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    job_dir = root / "p2p_alice" / "jobs" / "job_pathological_json"
    job_dir.mkdir(parents=True)
    (job_dir / "status.json").write_text(
        '{"status":' + ("9" * 5000) + "}",
        encoding="utf-8",
    )

    response = operations.jobs(_inst(root))

    assert response["count"] == 1
    assert response["jobs"][0]["status"] == "unknown"


def test_jobs_missing_workspace_returns_empty_diagnostics(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    resp = operations.jobs(_inst(missing))

    assert resp["workspace_root"] == str(missing)
    assert resp["workspace_exists"] is False
    assert resp["count"] == 0
    assert resp["jobs"] == []


def test_tasks_scan_p2p_and_group_workspaces_sorted_by_latest_update(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    _write_json(
        root / "p2p_alice" / "tasks" / "task_old" / "task.json",
        {
            "schema_version": 2,
            "task_id": "task_old",
            "description": "old question",
            "progress": "done",
            "status": "succeeded",
            "submitter": "Alice",
            "asked_at": 100,
            "updated_at": 120,
            "tools": [],
            "job_ids": [],
            "workspace": {"user_name": "Alice", "user_id": "ou_alice"},
        },
    )
    _write_json(
        root / "group_oc_room" / "user_bob" / "tasks" / "task_new" / "task.json",
        {
            "schema_version": 2,
            "task_id": "task_new",
            "description": "new question",
            "progress": "running",
            "status": "running",
            "submitter": "Bob",
            "asked_at": 200,
            "updated_at": 260,
            "tools": [{"name": "external_diff", "status": "running"}],
            "usage_totals": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "cached_tokens": 400,
                "cache_read_tokens": 400,
                "cache_write_tokens": 0,
                "llm_calls": 2,
                "cache_hit_calls": 1,
                "cache_hit_rate": 0.4,
                "cache_hit_call_rate": 0.5,
            },
            "llm_calls": [{"model": "test-model", "iteration": 0}],
            "job_ids": ["job_20260605_120000_deadbeef"],
            "workspace": {"user_name": "Bob", "user_id": "ou_bob"},
        },
    )

    resp = operations.tasks(_inst(root))

    assert resp["workspace_exists"] is True
    assert resp["count"] == 2
    assert [task["task_id"] for task in resp["tasks"]] == ["task_new", "task_old"]
    assert resp["tasks"][0]["submitter"] == "Bob"
    assert "tools" not in resp["tasks"][0]
    assert resp["tasks"][0]["usage_totals"]["cached_tokens"] == 400
    assert "llm_calls" not in resp["tasks"][0]
    assert resp["tasks"][0]["job_ids"] == ["job_20260605_120000_deadbeef"]


def test_tasks_scan_protected_group_actor_records_and_link_protected_jobs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    group_id = "30003"
    user_id = "20002"
    actor_digest = hashlib.sha256(
        f"group\0{group_id}\0{user_id}".encode("utf-8")
    ).hexdigest()
    state_root = root / f"group_{group_id}" / ".conversation-state"
    task_id = "task_group_protected"
    job_id = "job_20260818_221500_deadbeef"
    task_dir = (
        state_root
        / "task-actors"
        / actor_digest
        / "tasks"
        / task_id
    )
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": task_id,
            "description": "group persona request",
            "status": "running",
            "asked_at": 100,
            "updated_at": 110,
            "steps": [],
            "job_ids": [job_id],
            "workspace": {
                "chat_kind": "group",
                "chat_id": group_id,
                "user_id": None,
                "actor_ref": "qq:actor:test",
            },
        },
    )
    (task_dir / "events.jsonl").write_text(
        json.dumps(
            {"event": "task_started", "sequence": 1, "recorded_at": 100}
        )
        + "\n",
        encoding="utf-8",
    )
    job_dir = state_root / "jobs" / actor_digest / job_id
    _write_json(job_dir / "request.json", {"job_id": job_id})
    _write_json(
        job_dir / "status.json",
        {
            "status": "running",
            "stage": "testing",
            "message": "tests",
            "created_at": 101,
            "updated_at": 105,
        },
    )

    listing = operations.tasks(_inst(root))
    detail = operations.task_detail(_inst(root), task_id)
    events = operations.task_events(_inst(root), task_id)

    assert listing["count"] == 1
    assert listing["tasks"][0]["task_id"] == task_id
    assert detail is not None
    assert [step["type"] for step in detail["steps"]] == ["background_job"]
    assert detail["job_statuses"] == [
        {
            "job_id": job_id,
            "status": "running",
            "stage": "testing",
            "message": "tests",
            "error_code": "",
        }
    ]
    assert events is not None
    assert [event["event"] for event in events["events"]] == ["task_started"]


def test_tasks_scan_redacted_group_identity_intake_records(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_group_identity_rejected"
    task_dir = (
        root
        / "group_30003"
        / ".conversation-state"
        / "task-intake"
        / "tasks"
        / task_id
    )
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": task_id,
            "description": "（入站消息内容未保存：身份校验失败）",
            "progress": "已拒绝缺少可信身份的入站消息。",
            "status": "failed",
            "submitter": "未验证来源",
            "asked_at": 100,
            "updated_at": 101,
            "steps": [],
            "job_ids": [],
        },
    )
    _write_json(
        task_dir / "turn.json",
        {
            "task_id": task_id,
            "status": "failed",
            "stop_reason": "qq_sender_envelope_missing",
            "error": "qq_sender_envelope_missing",
        },
    )

    listing = operations.tasks(_inst(root))
    detail = operations.task_detail(_inst(root), task_id)

    assert listing["count"] == 1
    assert listing["tasks"][0]["submitter"] == "未验证来源"
    assert listing["tasks"][0]["description"] == "（入站消息内容未保存：身份校验失败）"
    assert detail is not None
    assert detail["task_id"] == task_id
    assert detail["status"] == "failed"


def test_tasks_include_corrupt_json_without_crashing(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_dir = root / "p2p_alice" / "tasks" / "task_corrupt"
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text("{bad json", encoding="utf-8")

    resp = operations.tasks(_inst(root))

    assert resp["count"] == 0
    assert resp["tasks"] == []


def test_tasks_missing_workspace_returns_empty_diagnostics(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    resp = operations.tasks(_inst(missing))

    assert resp["workspace_root"] == str(missing)
    assert resp["workspace_exists"] is False
    assert resp["count"] == 0
    assert resp["tasks"] == []


def test_tasks_exclude_legacy_and_limit_to_latest_50(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    _write_json(
        root / "p2p_alice" / "tasks" / "task_legacy" / "task.json",
        {"task_id": "task_legacy", "status": "succeeded", "updated_at": 9999},
    )
    for index in range(55):
        task_id = f"task_v2_{index:02d}"
        _write_json(
            root / "p2p_alice" / "tasks" / task_id / "task.json",
            {
                "schema_version": 2,
                "task_id": task_id,
                "description": task_id,
                "status": "succeeded",
                "updated_at": index + 1,
            },
        )

    resp = operations.tasks(_inst(root), limit=500)

    assert resp["count"] == 50
    assert len(resp["tasks"]) == 50
    assert resp["tasks"][0]["task_id"] == "task_v2_54"
    assert all(task["task_id"] != "task_legacy" for task in resp["tasks"])


def test_delete_task_removes_one_terminal_record_and_preserves_job_and_sibling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    actor_root = root / "p2p_alice"
    deleted_dir = actor_root / "tasks" / "task_delete"
    sibling_dir = actor_root / "tasks" / "task_keep"
    job_dir = actor_root / "jobs" / "job_20260822_120000_deadbeef"
    _write_json(
        deleted_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": "task_delete",
            "status": "succeeded",
            "job_ids": [job_dir.name],
        },
    )
    _write_json(deleted_dir / "contexts" / "ctx_one.json", {"safe": True})
    (deleted_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    outside_dir = tmp_path / "outside-artifacts"
    outside_dir.mkdir()
    (outside_dir / "keep.txt").write_text("keep", encoding="utf-8")
    (deleted_dir / "outside-link").symlink_to(outside_dir, target_is_directory=True)
    _write_json(
        sibling_dir / "task.json",
        {"schema_version": 2, "task_id": "task_keep", "status": "failed"},
    )
    _write_json(job_dir / "request.json", {"job_id": job_dir.name})
    _write_json(job_dir / "status.json", {"status": "succeeded"})
    _secure_task_record(deleted_dir)
    _secure_task_record(sibling_dir)

    result = operations.delete_task(_inst(root), "task_delete")

    assert result == {
        "ok": True,
        "deleted": True,
        "task_id": "task_delete",
        "status": "succeeded",
    }
    assert not deleted_dir.exists()
    assert sibling_dir.is_dir()
    assert job_dir.is_dir()
    assert (outside_dir / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert operations.task_detail(_inst(root), "task_delete") is None


def test_delete_task_rejects_active_or_unknown_status_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    active_dir = root / "p2p_alice" / "tasks" / "task_active"
    unknown_dir = root / "p2p_alice" / "tasks" / "task_unknown"
    _write_json(
        active_dir / "task.json",
        {"schema_version": 2, "task_id": "task_active", "status": "running"},
    )
    _write_json(
        unknown_dir / "task.json",
        {"schema_version": 2, "task_id": "task_unknown", "status": "unknown"},
    )
    _secure_task_record(active_dir)
    _secure_task_record(unknown_dir)

    with pytest.raises(observability.TaskDeletionConflictError, match="active tasks"):
        operations.delete_task(_inst(root), "task_active")
    with pytest.raises(observability.UnsafeTaskRecordError, match="terminal state"):
        operations.delete_task(_inst(root), "task_unknown")

    assert active_dir.is_dir()
    assert unknown_dir.is_dir()


@pytest.mark.parametrize("job_status", sorted(CODE_TASK_ACTIVE_STATUSES))
def test_delete_task_rejects_terminal_record_with_active_background_job(
    tmp_path: Path,
    job_status: str,
) -> None:
    root = tmp_path / "workspaces"
    actor_root = root / "p2p_alice"
    job_id = "job_20260822_120001_deadbeef"
    task_dir = actor_root / "tasks" / "task_job_active"
    job_dir = actor_root / "jobs" / job_id
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": "task_job_active",
            "status": "failed",
            "job_ids": [job_id],
        },
    )
    _write_json(job_dir / "request.json", {"job_id": job_id})
    _write_json(job_dir / "status.json", {"status": job_status})
    _secure_task_record(task_dir)

    with pytest.raises(observability.TaskDeletionConflictError, match="background jobs"):
        operations.delete_task(_inst(root), "task_job_active")

    assert task_dir.is_dir()
    assert job_dir.is_dir()


@pytest.mark.parametrize("job_status", ["", "unknown"])
def test_delete_task_rejects_unverified_background_job_status(
    tmp_path: Path,
    job_status: str,
) -> None:
    root = tmp_path / "workspaces"
    actor_root = root / "p2p_alice"
    job_id = "job_20260822_120002_deadbeef"
    task_dir = actor_root / "tasks" / "task_job_unknown"
    job_dir = actor_root / "jobs" / job_id
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": "task_job_unknown",
            "status": "failed",
            "job_ids": [job_id],
        },
    )
    _write_json(job_dir / "request.json", {"job_id": job_id})
    if job_status:
        _write_json(job_dir / "status.json", {"status": job_status})
    _secure_task_record(task_dir)

    with pytest.raises(observability.UnsafeTaskRecordError, match="background job status"):
        operations.delete_task(_inst(root), "task_job_unknown")

    assert task_dir.is_dir()
    assert job_dir.is_dir()


def test_delete_task_rejects_malformed_background_job_references(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_dir = root / "p2p_alice" / "tasks" / "task_job_refs"
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": "task_job_refs",
            "status": "failed",
            "job_ids": "job_not_a_list",
        },
    )
    _secure_task_record(task_dir)

    with pytest.raises(observability.UnsafeTaskRecordError, match="references are malformed"):
        operations.delete_task(_inst(root), "task_job_refs")

    assert task_dir.is_dir()


def test_delete_task_rejects_unsafe_metadata_and_directory_permissions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    outside = tmp_path / "outside-task.json"
    _write_json(
        outside,
        {"schema_version": 2, "task_id": "task_symlink", "status": "failed"},
    )
    task_dir = root / "p2p_alice" / "tasks" / "task_symlink"
    task_dir.mkdir(parents=True)
    task_dir.parent.chmod(0o700)
    task_dir.chmod(0o700)
    (task_dir / "task.json").symlink_to(outside)

    with pytest.raises(observability.UnsafeTaskRecordError, match="metadata"):
        operations.delete_task(_inst(root), "task_symlink")
    assert outside.is_file()
    assert task_dir.is_dir()

    (task_dir / "task.json").unlink()
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": "task_symlink", "status": "failed"},
    )
    task_dir.chmod(0o755)
    with pytest.raises(observability.UnsafeTaskRecordError, match="directory is unsafe"):
        operations.delete_task(_inst(root), "task_symlink")
    assert task_dir.is_dir()


def test_delete_task_rejects_ambiguous_instance_records(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    for actor in ("p2p_alice", "p2p_bob"):
        task_dir = root / actor / "tasks" / "task_duplicate"
        _write_json(
            task_dir / "task.json",
            {"schema_version": 2, "task_id": "task_duplicate", "status": "failed"},
        )
        _secure_task_record(task_dir)

    with pytest.raises(observability.UnsafeTaskRecordError, match="multiple records"):
        operations.delete_task(_inst(root), "task_duplicate")
    assert len(tuple(root.glob("**/tasks/task_duplicate/task.json"))) == 2


def test_delete_task_rejects_live_event_writer_then_succeeds(tmp_path: Path) -> None:
    import fcntl

    root = tmp_path / "workspaces"
    task_dir = root / "p2p_alice" / "tasks" / "task_locked"
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": "task_locked", "status": "succeeded"},
    )
    (task_dir / ".events.lock").write_text("", encoding="utf-8")
    _secure_task_record(task_dir)
    descriptor = os.open(task_dir / ".events.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(observability.TaskDeletionConflictError, match="still being written"):
            operations.delete_task(_inst(root), "task_locked")
        assert task_dir.is_dir()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert operations.delete_task(_inst(root), "task_locked") is not None
    assert not task_dir.exists()


def test_delete_task_route_maps_success_conflict_missing_and_invalid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspaces"
    finished = root / "p2p_alice" / "tasks" / "task_route_delete"
    active = root / "p2p_alice" / "tasks" / "task_route_active"
    _write_json(
        finished / "task.json",
        {"schema_version": 2, "task_id": finished.name, "status": "failed"},
    )
    _write_json(
        active / "task.json",
        {"schema_version": 2, "task_id": active.name, "status": "delegated"},
    )
    _secure_task_record(finished)
    _secure_task_record(active)
    monkeypatch.setattr(bot_routes, "get_instance", lambda _instance_id: _inst(root))

    result = bot_routes.delete_bot_task("sample-bot", finished.name)
    assert result["deleted"] is True

    with pytest.raises(HTTPException) as active_error:
        bot_routes.delete_bot_task("sample-bot", active.name)
    assert active_error.value.status_code == 409
    with pytest.raises(HTTPException) as missing_error:
        bot_routes.delete_bot_task("sample-bot", "task_missing")
    assert missing_error.value.status_code == 404
    with pytest.raises(HTTPException) as invalid_error:
        bot_routes.delete_bot_task("sample-bot", "../outside")
    assert invalid_error.value.status_code == 400


def test_task_detail_merges_job_stages_and_events(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_detail"
    job_id = "job_20260724_120000_deadbeef"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": task_id,
            "description": "observe",
            "status": "running",
            "started_at": 100,
            "updated_at": 110,
            "steps": [
                {
                    "step_id": "tool-1",
                    "type": "tool",
                    "depth": 0,
                    "title": "code_task",
                    "summary": job_id,
                    "elapsed_s": 1,
                }
            ],
            "job_ids": [job_id],
            "context_snapshots": [
                {
                    "snapshot_id": "ctx_call_1",
                    "backend": "codex",
                    "model": "gpt-5",
                    "iteration": 0,
                    "coverage": "adapter_visible",
                    "capture_status": "captured",
                    "redacted": True,
                    "truncated": False,
                    "captured_at": 102,
                    "message_count": 3,
                    "effective_message_count": 1,
                    "tool_schema_count": 2,
                    "resource_count": 1,
                    "estimated_tokens": 456,
                    "reasoning_effort": "high",
                    "context_kind": "provider_managed_resume",
                    "omitted": ["provider_managed_resume_context"],
                    "trace_id": "trace-1",
                    "span_id": "llm-1",
                    "parent_span_id": "subagent-1",
                    "depth": 2,
                    "role": "subagent",
                    "unexpected_private_field": "must not be projected",
                },
                {"snapshot_id": "../invalid"},
            ],
        },
    )
    (task_dir / "events.jsonl").write_text(
        json.dumps({"event": "task_started", "recorded_at": 100, "data": {}}) + "\n{bad\n",
        encoding="utf-8",
    )
    job_dir = root / "p2p_alice" / "jobs" / job_id
    _write_json(job_dir / "request.json", {"job_id": job_id})
    _write_json(
        job_dir / "status.json",
        {
            "status": "running",
            "stage": "testing",
            "message": "tests",
            "created_at": 101,
            "updated_at": 105,
        },
    )
    (job_dir / "status-events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "job_stage_changed",
                        "recorded_at": 101,
                        "data": {"status": "running", "stage": "coding", "message": "code"},
                    }
                ),
                json.dumps(
                    {
                        "event": "job_stage_changed",
                        "recorded_at": 103,
                        "data": {"status": "running", "stage": "testing", "message": "tests"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    detail = operations.task_detail(_inst(root), task_id)
    events = operations.task_events(_inst(root), task_id)

    assert detail is not None
    assert [step["type"] for step in detail["steps"]] == [
        "tool",
        "background_job",
        "job_stage",
        "job_stage",
    ]
    assert detail["steps"][1]["parent_step_id"] == "tool-1"
    assert detail["steps"][2]["elapsed_s"] == 2
    assert detail["context_snapshots"] == [
        {
            "snapshot_id": "ctx_call_1",
            "backend": "codex",
            "model": "gpt-5",
            "iteration": 0,
            "coverage": "adapter_visible",
            "capture_status": "captured",
            "redacted": True,
            "truncated": False,
            "captured_at": 102.0,
            "message_count": 3,
            "effective_message_count": 1,
            "tool_schema_count": 2,
            "resource_count": 1,
            "estimated_tokens": 456,
            "reasoning_effort": "high",
            "context_kind": "provider_managed_resume",
            "omitted": ["provider_managed_resume_context"],
            "trace_id": "trace-1",
            "span_id": "llm-1",
            "parent_span_id": "subagent-1",
            "depth": 2,
            "role": "subagent",
        }
    ]
    assert events is not None
    assert events["count"] == 3
    assert events["events"][-1]["job_id"] == job_id


def test_console_redacts_legacy_job_status_event_and_stdout_payloads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_legacy_job_secret"
    job_id = "job_20260818_120000_deadbeef"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    encoded_value = "".join(("dXNl", "cjpw", "YXNz", "d29y", "ZA=="))
    auth_message = "Authorization: " + "Basic " + encoded_value
    env_value = "".join(("console", "-stdout-", "credential-value"))
    uri_password = "".join(("uri", "-credential-", "value"))
    monkeypatch.setenv("CONSOLE_STDOUT_" + "API_" + "TOKEN", env_value)
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": task_id,
            "status": "running",
            "job_ids": [job_id],
            "steps": [],
        },
    )
    job_dir = root / "p2p_alice" / "jobs" / job_id
    _write_json(job_dir / "request.json", {"job_id": job_id})
    _write_json(
        job_dir / "status.json",
        {
            "status": "running",
            "stage": "coding",
            "message": auth_message,
            "created_at": 100,
            "updated_at": 101,
        },
    )
    (job_dir / "status-events.jsonl").write_text(
        json.dumps(
            {
                "event": "job_stage_changed",
                "recorded_at": 100,
                "data": {
                    "status": "running",
                    "stage": "coding",
                    "message": auth_message,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (job_dir / "stdout.log").write_text(
        "\n".join(
            (
                auth_message,
                "https://" + "worker:" + uri_password + "@example.test/path",
                f"token={env_value}",
                f"working in {job_dir}",
            )
        ),
        encoding="utf-8",
    )

    detail = operations.task_detail(_inst(root), task_id)
    events = operations.task_events(_inst(root), task_id)
    jobs = operations.jobs(_inst(root))

    assert detail is not None
    assert events is not None
    serialized_detail = json.dumps(detail)
    serialized_events = json.dumps(events)
    assert encoded_value not in serialized_detail
    assert encoded_value not in serialized_events
    assert "[REDACTED]" in serialized_detail
    assert "[REDACTED]" in serialized_events
    assert events["events"][0]["sanitization"]["redacted_for_console"] is True
    progress_tail = str(jobs["jobs"][0]["progress_tail"])
    assert encoded_value not in progress_tail
    assert uri_password not in progress_tail
    assert env_value not in progress_tail
    assert str(job_dir) not in progress_tail
    assert "[REDACTED]" in progress_tail
    assert "$WORKSPACE" in progress_tail


def test_task_job_symlink_alias_cannot_rebind_status_or_events(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_job_alias"
    requested_job_id = "job_20260818_120001_deadbeef"
    target_job_id = "job_20260818_120002_feedface"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": task_id,
            "status": "running",
            "job_ids": [requested_job_id],
            "steps": [],
        },
    )
    target_dir = root / "p2p_alice" / "jobs" / target_job_id
    _write_json(target_dir / "request.json", {"job_id": target_job_id})
    _write_json(
        target_dir / "status.json",
        {"status": "running", "message": "TARGET_JOB_STATUS"},
    )
    (target_dir / "status-events.jsonl").write_text(
        json.dumps(
            {
                "event": "job_stage_changed",
                "recorded_at": 1,
                "data": {"message": "TARGET_JOB_EVENT"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    alias = target_dir.parent / requested_job_id
    alias.symlink_to(target_dir.name, target_is_directory=True)

    assert observability._find_job_dir(task_dir, requested_job_id) is None
    detail = operations.task_detail(_inst(root), task_id)
    events = operations.task_events(_inst(root), task_id)

    assert detail is not None
    assert events is not None
    serialized = json.dumps({"detail": detail, "events": events})
    assert "TARGET_JOB_STATUS" not in serialized
    assert "TARGET_JOB_EVENT" not in serialized
    assert all(step.get("title") != requested_job_id for step in detail["steps"])

    alias.unlink()
    _write_json(alias / "request.json", {"job_id": target_job_id})
    _write_json(alias / "status.json", {"status": "running"})
    assert observability._find_job_dir(task_dir, requested_job_id) is None


def test_task_detail_rejects_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    try:
        operations.task_detail(_inst(root), "../task_escape")
    except ValueError as exc:
        assert "invalid task id" in str(exc)
    else:
        raise AssertionError("path traversal must be rejected")


def test_task_events_returns_a_bounded_tail(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_many_events"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": task_id,
            "status": "running",
        },
    )
    (task_dir / "events.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "event_id": f"{task_id}:{index}",
                    "sequence": index,
                    "event": "activity",
                    "recorded_at": float(index),
                    "data": {"index": index},
                }
            )
            for index in range(1, 701)
        )
        + "\n",
        encoding="utf-8",
    )

    events = operations.task_events(_inst(root), task_id, limit=25)

    assert events is not None
    assert events["count"] == 25
    assert events["limit"] == 25
    assert events["truncated"] is True
    assert events["events"][0]["sequence"] == 676
    assert events["events"][-1]["sequence"] == 700


def test_task_events_skip_json_numbers_over_parser_limit(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_pathological_event"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": task_id, "status": "running"},
    )
    (task_dir / "events.jsonl").write_text(
        '{"sequence":' + ("9" * 5000) + "}\n"
        + json.dumps(
            {
                "event_id": f"{task_id}:1",
                "sequence": 1,
                "event": "valid",
                "recorded_at": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events = operations.task_events(_inst(root), task_id)

    assert events is not None
    assert [event["event"] for event in events["events"]] == ["valid"]
    assert events["truncated"] is True
    assert events["integrity_gap"] is True


def test_task_event_sequence_survives_wall_clock_rollback(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_clock_rollback"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": task_id, "status": "running"},
    )
    (task_dir / "events.jsonl").write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "event_id": f"{task_id}:1",
                        "sequence": 1,
                        "event": "first",
                        "recorded_at": 200,
                    }
                ),
                json.dumps(
                    {
                        "event_id": f"{task_id}:2",
                        "sequence": 2,
                        "event": "second",
                        "recorded_at": 100,
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    events = operations.task_events(_inst(root), task_id)

    assert events is not None
    assert [event["sequence"] for event in events["events"]] == [1, 2]


def test_task_events_rejects_symlinked_event_log(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_symlinked_events"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": task_id, "status": "running"},
    )
    external = tmp_path / "external-events.jsonl"
    external.write_text(
        json.dumps({"event": "secret", "recorded_at": 100}) + "\n",
        encoding="utf-8",
    )
    (task_dir / "events.jsonl").symlink_to(external)

    events = operations.task_events(_inst(root), task_id)

    assert events is not None
    assert events["events"] == []
    assert events["truncated"] is True
    assert events["integrity_gap"] is True


def test_event_tail_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    external = tmp_path / "external-event-parent"
    external.mkdir()
    (external / "status-events.jsonl").write_text(
        json.dumps({"event": "PRIVATE_JOB_EVENT", "recorded_at": 1}) + "\n",
        encoding="utf-8",
    )
    alias = tmp_path / "job_event_alias"
    alias.symlink_to(external, target_is_directory=True)

    events, truncated, integrity_gap = observability._read_json_lines_tail(
        alias / "status-events.jsonl",
        limit=10,
    )

    assert events == []
    assert truncated is True
    assert integrity_gap is True


def test_task_events_holds_directory_fd_during_ancestor_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_event_ancestor_swap"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": task_id, "status": "running"},
    )
    (task_dir / "events.jsonl").write_text(
        json.dumps({"event": "safe", "recorded_at": 1}) + "\n",
        encoding="utf-8",
    )
    external = tmp_path / "external-event-task"
    external.mkdir()
    (external / "events.jsonl").write_text(
        json.dumps({"event": "PRIVATE_REDIRECTED_EVENT", "recorded_at": 2}) + "\n",
        encoding="utf-8",
    )
    held_task_dir = task_dir.with_name(f"{task_id}-held")
    real_stat = observability.os.stat
    swapped = False

    def race_stat(path, *args, **kwargs):
        nonlocal swapped
        current = real_stat(path, *args, **kwargs)
        if (
            not swapped
            and path == "events.jsonl"
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            task_dir.rename(held_task_dir)
            task_dir.symlink_to(external, target_is_directory=True)
            swapped = True
        return current

    monkeypatch.setattr(observability.os, "stat", race_stat)

    response = operations.task_events(_inst(root), task_id)

    assert swapped is True
    assert response is not None
    assert [event["event"] for event in response["events"]] == ["safe"]
    assert "PRIVATE_REDIRECTED_EVENT" not in json.dumps(response)


def test_task_events_reports_unsafe_permissions_and_partial_tail(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_unsafe_event_integrity"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": task_id, "status": "running"},
    )
    event_path = task_dir / "events.jsonl"
    valid = json.dumps(
        {
            "event_id": f"{task_id}:1",
            "sequence": 1,
            "event": "valid",
            "recorded_at": 1,
        }
    )
    event_path.write_text(valid + "\n", encoding="utf-8")
    event_path.chmod(0o666)

    unsafe = operations.task_events(_inst(root), task_id)

    assert unsafe is not None
    assert unsafe["events"] == []
    assert unsafe["truncated"] is True
    assert unsafe["integrity_gap"] is True

    event_path.chmod(0o600)
    event_path.write_text(valid + "\n" + '{"sequence":2', encoding="utf-8")
    partial = operations.task_events(_inst(root), task_id)

    assert partial is not None
    assert [event["event"] for event in partial["events"]] == ["valid"]
    assert partial["truncated"] is True
    assert partial["integrity_gap"] is True


def test_task_events_reports_sequence_gap(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    task_id = "task_event_sequence_gap"
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {"schema_version": 2, "task_id": task_id, "status": "running"},
    )
    (task_dir / "events.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "event_id": f"{task_id}:{sequence}",
                    "sequence": sequence,
                    "event": f"event-{sequence}",
                    "recorded_at": sequence,
                }
            )
            for sequence in (1, 3)
        )
        + "\n",
        encoding="utf-8",
    )

    events = operations.task_events(_inst(root), task_id)

    assert events is not None
    assert [event["sequence"] for event in events["events"]] == [1, 3]
    assert events["truncated"] is True
    assert events["integrity_gap"] is True


def _make_context_snapshot(
    root: Path,
    *,
    task_id: str = "task_context",
    snapshot_id: str = "ctx_call_1",
) -> tuple[BotInstance, Path]:
    task_dir = root / "p2p_alice" / "tasks" / task_id
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 2,
            "task_id": task_id,
            "description": "context",
            "status": "succeeded",
            "context_snapshots": [{"snapshot_id": snapshot_id}],
        },
    )
    snapshot_path = task_dir / "contexts" / f"{snapshot_id}.json"
    _write_json(
        snapshot_path,
        {
            "schema_version": 1,
            "task_id": task_id,
            "snapshot_id": snapshot_id,
            "coverage": "exact_model_input",
            "session_messages": [{"role": "user", "content": "hello"}],
            "effective_messages": [{"role": "user", "content": "hello"}],
            "tool_schemas": [],
            "resources": [],
            "sanitization": {
                "redacted_before_persistence": True,
                "redacted": False,
                "replacement_count": 0,
            },
        },
    )
    snapshot_path.parent.chmod(0o700)
    snapshot_path.chmod(0o600)
    return _inst(root), snapshot_path


def test_context_snapshot_reads_task_bound_regular_json(tmp_path: Path) -> None:
    inst, _ = _make_context_snapshot(tmp_path / "workspaces")

    snapshot = observability.context_snapshot(inst, "task_context", "ctx_call_1")

    assert snapshot is not None
    assert snapshot["task_id"] == "task_context"
    assert snapshot["snapshot_id"] == "ctx_call_1"
    assert snapshot["coverage"] == "exact_model_input"
    assert snapshot["sanitization"]["redacted_for_console"] is True


def test_context_snapshot_redacts_new_environment_secrets_on_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inst, snapshot_path = _make_context_snapshot(tmp_path / "workspaces")
    late_value = "".join(("late", "-context-", "credential-value"))
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["session_messages"][0]["content"] = late_value
    _write_json(snapshot_path, payload)
    snapshot_path.chmod(0o600)
    monkeypatch.setenv("LATE_CONTEXT_" + "API_" + "TOKEN", late_value)

    snapshot = observability.context_snapshot(inst, "task_context", "ctx_call_1")

    assert snapshot is not None
    assert late_value not in json.dumps(snapshot)
    assert snapshot["sanitization"]["redacted_for_console"] is True
    assert snapshot["sanitization"]["console_replacement_count"] == 1


def test_context_snapshot_route_is_no_store(tmp_path: Path, monkeypatch) -> None:
    inst, _ = _make_context_snapshot(tmp_path / "workspaces")
    monkeypatch.setattr(bot_routes, "get_instance", lambda _instance_id: inst)
    response = Response()

    snapshot = bot_routes.bot_task_context(
        "sample-bot",
        "task_context",
        "ctx_call_1",
        response,
    )

    assert snapshot["snapshot_id"] == "ctx_call_1"
    assert response.headers["cache-control"] == "no-store"


def test_context_snapshot_rejects_invalid_id_and_identity_mismatch(tmp_path: Path) -> None:
    inst, snapshot_path = _make_context_snapshot(tmp_path / "workspaces")

    try:
        observability.context_snapshot(inst, "task_context", "../ctx_call_1")
    except ValueError as exc:
        assert "invalid context snapshot id" in str(exc)
    else:
        raise AssertionError("context path traversal must be rejected")

    _write_json(
        snapshot_path,
        {"task_id": "task_other", "snapshot_id": "ctx_call_1"},
    )
    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "another task" in str(exc)
    else:
        raise AssertionError("cross-task context artifacts must be rejected")

    _write_json(
        snapshot_path,
        {"task_id": "task_context", "snapshot_id": "ctx_call_1"},
    )
    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "redaction provenance" in str(exc)
    else:
        raise AssertionError("unattested context artifacts must be rejected")


def test_context_snapshot_rejects_symlink_non_regular_and_oversize_files(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    inst, snapshot_path = _make_context_snapshot(root)
    outside = tmp_path / "outside.json"
    _write_json(outside, {"snapshot_id": "ctx_call_1"})

    snapshot_path.unlink()
    snapshot_path.symlink_to(outside)
    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "regular file" in str(exc)
    else:
        raise AssertionError("symlink context artifacts must be rejected")

    snapshot_path.unlink()
    snapshot_path.mkdir()
    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "regular file" in str(exc)
    else:
        raise AssertionError("directory context artifacts must be rejected")

    snapshot_path.rmdir()
    with snapshot_path.open("wb") as handle:
        handle.truncate(8 * 1024 * 1024 + 1)
    snapshot_path.chmod(0o600)
    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "8 MiB" in str(exc)
    else:
        raise AssertionError("oversize context artifacts must be rejected")


def test_context_snapshot_rejects_symlinked_context_directory(tmp_path: Path) -> None:
    inst, snapshot_path = _make_context_snapshot(tmp_path / "workspaces")
    contexts_dir = snapshot_path.parent
    held_contexts_dir = contexts_dir.with_name("contexts-held")
    contexts_dir.rename(held_contexts_dir)
    contexts_dir.symlink_to(held_contexts_dir, target_is_directory=True)

    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "directory is unsafe" in str(exc)
    else:
        raise AssertionError("symlinked context directories must be rejected")


def test_context_snapshot_holds_directory_fd_during_ancestor_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inst, snapshot_path = _make_context_snapshot(tmp_path / "workspaces")
    contexts_dir = snapshot_path.parent
    external = tmp_path / "external-contexts"
    external_snapshot = external / snapshot_path.name
    _write_json(
        external_snapshot,
        {
            "schema_version": 1,
            "task_id": "task_context",
            "snapshot_id": "ctx_call_1",
            "coverage": "exact_model_input",
            "session_messages": [
                {"role": "user", "content": "PRIVATE_REDIRECTED_CONTEXT"}
            ],
            "effective_messages": [],
            "tool_schemas": [],
            "resources": [],
            "sanitization": {"redacted_before_persistence": True},
        },
    )
    external.chmod(0o700)
    external_snapshot.chmod(0o600)
    held_contexts_dir = contexts_dir.with_name("contexts-held")
    real_stat = observability.os.stat
    swapped = False

    def race_stat(path, *args, **kwargs):
        nonlocal swapped
        current = real_stat(path, *args, **kwargs)
        if (
            not swapped
            and path == snapshot_path.name
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            contexts_dir.rename(held_contexts_dir)
            contexts_dir.symlink_to(external, target_is_directory=True)
            swapped = True
        return current

    monkeypatch.setattr(observability.os, "stat", race_stat)

    snapshot = observability.context_snapshot(inst, "task_context", "ctx_call_1")

    assert swapped is True
    assert snapshot is not None
    serialized = json.dumps(snapshot)
    assert "hello" in serialized
    assert "PRIVATE_REDIRECTED_CONTEXT" not in serialized


def test_context_snapshot_rejects_multiple_hard_links(tmp_path: Path) -> None:
    inst, snapshot_path = _make_context_snapshot(tmp_path / "workspaces")
    alias = tmp_path / "snapshot-alias.json"
    alias.hardlink_to(snapshot_path)

    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "exactly one hard link" in str(exc)
    else:
        raise AssertionError("hard-linked context artifacts must be rejected")


def test_context_snapshot_rejects_json_number_over_parser_limit(tmp_path: Path) -> None:
    inst, snapshot_path = _make_context_snapshot(tmp_path / "workspaces")
    snapshot_path.write_text(
        '{"task_id":"task_context","snapshot_id":"ctx_call_1",'
        '"sanitization":{"redacted_before_persistence":true},"count":'
        + ("9" * 5000)
        + "}",
        encoding="utf-8",
    )
    snapshot_path.chmod(0o600)

    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "valid UTF-8 JSON" in str(exc)
    else:
        raise AssertionError("pathological context JSON must be rejected")


def test_context_snapshot_rejects_shallow_wide_json_before_parsing(
    tmp_path: Path,
) -> None:
    inst, snapshot_path = _make_context_snapshot(tmp_path / "workspaces")
    snapshot_path.write_bytes(
        b'{"task_id":"task_context","snapshot_id":"ctx_call_1",'
        b'"sanitization":{"redacted_before_persistence":true},"items":['
        + b",".join([b"0"] * 200_010)
        + b"]}"
    )
    snapshot_path.chmod(0o600)

    try:
        observability.context_snapshot(inst, "task_context", "ctx_call_1")
    except observability.UnsafeContextSnapshotError as exc:
        assert "parsing budget" in str(exc)
    else:
        raise AssertionError("shallow-wide context JSON must be rejected")
