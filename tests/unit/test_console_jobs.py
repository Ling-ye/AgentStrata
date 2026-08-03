import json
from pathlib import Path

from console.control import operations
from console.control.instances import BotInstance


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
    assert resp["tasks"][0]["tools"][0]["name"] == "external_diff"
    assert resp["tasks"][0]["usage_totals"]["cached_tokens"] == 400
    assert resp["tasks"][0]["llm_calls"][0]["model"] == "test-model"
    assert resp["tasks"][0]["job_ids"] == ["job_20260605_120000_deadbeef"]


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
        },
    )
    (task_dir / "events.jsonl").write_text(
        json.dumps({"event": "task_started", "recorded_at": 100, "data": {}}) + "\n{bad\n",
        encoding="utf-8",
    )
    job_dir = root / "p2p_alice" / "jobs" / job_id
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
    assert events is not None
    assert events["count"] == 3
    assert events["events"][-1]["job_id"] == job_id


def test_task_detail_rejects_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    try:
        operations.task_detail(_inst(root), "../task_escape")
    except ValueError as exc:
        assert "invalid task id" in str(exc)
    else:
        raise AssertionError("path traversal must be rejected")
