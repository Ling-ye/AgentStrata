import json
from pathlib import Path

from chatcopilot.middleware.runtime.tasks import TurnTaskRecorder, complete_delegated_task
from chatcopilot.middleware.runtime.workspace import Workspace


def _workspace(root: Path) -> Workspace:
    return Workspace(
        root=root,
        chat_kind="p2p",
        chat_id="oc_test",
        user_id="ou_test",
        user_name="Alice",
    ).ensure()


def test_turn_task_records_plain_answer_without_tools(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-1",
        message_id="msg-1",
        user_text="你是谁，你能做什么",
    )

    recorder.finish(status="succeeded", progress="已完成回答。")
    payload = recorder.to_payload()

    assert payload["task_id"].startswith("task_")
    assert payload["description"] == "你是谁，你能做什么"
    assert payload["status"] == "succeeded"
    assert payload["submitter"] == "Alice"
    assert payload["tools"] == []
    assert payload["job_ids"] == []
    assert recorder.path.is_file()


def test_turn_task_records_tool_events_and_job_ids(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-2",
        message_id="msg-2",
        user_text="对比两个文件",
    )

    recorder.tool_started("external_diff", {"file1": "a.csv", "file2": "b.csv"})
    recorder.tool_finished(
        "external_diff",
        True,
        "已加入用户队列，任务 ID: job_20260605_120000_deadbeef。",
    )
    recorder.finish(status="succeeded", progress="已完成回答。")
    payload = recorder.to_payload()

    assert payload["tools"][0]["name"] == "external_diff"
    assert payload["tools"][0]["status"] == "succeeded"
    assert payload["tools"][0]["elapsed_s"] is not None
    assert payload["job_ids"] == ["job_20260605_120000_deadbeef"]


def test_turn_task_records_llm_usage_totals(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-3",
        message_id="msg-3",
        user_text="查一下缓存命中",
    )

    recorder.llm_call_finished(
        model="test-model",
        iteration=0,
        finish_reason="tool_calls",
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 120,
            "total_tokens": 1120,
            "cached_tokens": 250,
            "cache_read_tokens": 250,
            "cache_write_tokens": 0,
        },
        trace_id="trace-1",
        span_id="span-root",
        depth=0,
    )
    recorder.llm_call_finished(
        model="test-model",
        iteration=1,
        finish_reason="stop",
        usage={
            "prompt_tokens": 500,
            "completion_tokens": 80,
            "total_tokens": 580,
            "cached_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 100,
        },
        trace_id="trace-1",
        span_id="span-root",
        depth=0,
    )

    payload = recorder.to_payload()

    assert len(payload["llm_calls"]) == 2
    assert payload["llm_calls"][0]["model"] == "test-model"
    assert payload["usage_totals"]["llm_calls"] == 2
    assert payload["usage_totals"]["prompt_tokens"] == 1500
    assert payload["usage_totals"]["completion_tokens"] == 200
    assert payload["usage_totals"]["total_tokens"] == 1700
    assert payload["usage_totals"]["cached_tokens"] == 250
    assert payload["usage_totals"]["cache_read_tokens"] == 250
    assert payload["usage_totals"]["cache_write_tokens"] == 100
    assert payload["usage_totals"]["cache_hit_calls"] == 1
    assert payload["usage_totals"]["cache_hit_rate"] == 0.1667
    assert payload["usage_totals"]["cache_hit_call_rate"] == 0.5
    recorder.finish(status="succeeded", progress="usage recorded")


def test_delegated_task_follows_child_job_terminal_result(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "ws")
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="sid-delegated",
        message_id="msg-delegated",
        user_text="修改仓库中的 scripts/foo.py",
    )
    job_id = "job_20260716_210000_deadbeef"
    recorder.record_job_submitted(job_id)
    recorder.finish(
        status="delegated",
        progress="background job submitted",
        final_text="accepted",
        stop_reason="code_route_background",
    )

    delegated = recorder.to_payload()
    assert delegated["status"] == "delegated"
    assert delegated["job_ids"] == [job_id]
    assert delegated["turn_finished_at"] is not None
    assert delegated["finished_at"] is None

    completed = complete_delegated_task(
        workspace,
        task_id=recorder.task_id,
        job_id=job_id,
        result={
            "ok": False,
            "stage": "failed",
            "error_code": "scope_violation",
            "details": {"failed_stage": "validating"},
            "error": "outside scope",
            "outputs": [],
            "finished_at": 123.0,
        },
    )

    assert completed is not None
    assert completed["status"] == "failed"
    assert completed["finished_at"] is not None
    assert completed["job_results"][0]["stage"] == "validating"
    assert completed["job_results"][0]["error_code"] == "scope_violation"


def test_v2_nested_steps_aggregate_leaf_llm_usage_once(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-nested",
        message_id="msg-nested",
        user_text="nested",
    )
    recorder.tool_started("delegate", {}, span_id="tool", depth=0)
    recorder.span_started(
        "worker",
        "subagent",
        span_id="subagent",
        parent_span_id="tool",
        depth=1,
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=0,
        span_id="llm",
        parent_span_id="subagent",
        depth=2,
        input_estimated_tokens=80,
        context_kind="sliding_window",
    )
    recorder.llm_call_finished(
        model="test-model",
        iteration=0,
        span_id="llm",
        parent_span_id="subagent",
        depth=2,
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 40,
        },
        context_kind="sliding_window",
    )
    recorder.span_finished("worker", "subagent", True, "done", span_id="subagent", depth=1)
    recorder.tool_finished("delegate", True, "done", span_id="tool", depth=0)

    payload = recorder.to_payload()
    by_id = {step["step_id"]: step for step in payload["steps"]}

    assert payload["schema_version"] == 2
    assert payload["usage_totals"]["total_tokens"] == 120
    assert by_id["subagent"]["inclusive_usage"]["total_tokens"] == 120
    assert by_id["tool"]["inclusive_usage"]["total_tokens"] == 120
    assert by_id["tool"]["inclusive_usage"]["cached_tokens"] == 40


def test_failed_llm_call_closes_running_step(tmp_path: Path) -> None:
    recorder = TurnTaskRecorder(
        workspace=_workspace(tmp_path / "ws"),
        session_id="sid-failed",
        message_id="msg-failed",
        user_text="fail",
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=0,
        span_id="llm-failed",
        input_estimated_tokens=100,
        context_kind="sliding_window",
    )
    recorder.span_finished(
        "test-model",
        "llm",
        False,
        "network error",
        span_id="llm-failed",
    )

    step = recorder.to_payload()["steps"][0]
    assert step["status"] == "failed"
    assert step["finished_at"] is not None
    assert step["error"] == "network error"


def test_ready_task_forecast_is_fixed_after_first_calculation(tmp_path: Path) -> None:
    history_root = tmp_path / "instance"
    for index in range(20):
        task_path = history_root / "history" / "tasks" / f"task_hist_{index}" / "task.json"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "task_id": f"task_hist_{index}",
                    "status": "succeeded",
                    "finished_at": index + 1,
                    "primary_model": "test-model",
                    "context_kind": "sliding_window",
                    "usage_totals": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                    "llm_calls": [],
                }
            ),
            encoding="utf-8",
        )
    recorder = TurnTaskRecorder(
        workspace=_workspace(history_root / "live"),
        session_id="sid-fixed",
        message_id="msg-fixed",
        user_text="fixed",
        history_root=history_root,
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=0,
        span_id="llm-1",
        input_estimated_tokens=100,
        context_kind="sliding_window",
    )
    first = recorder.to_payload()["forecast"]

    changed_path = history_root / "history" / "tasks" / "task_hist_new" / "task.json"
    changed_path.parent.mkdir(parents=True, exist_ok=True)
    changed_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "task_id": "task_hist_new",
                "status": "succeeded",
                "finished_at": 999,
                "primary_model": "test-model",
                "context_kind": "sliding_window",
                "usage_totals": {
                    "prompt_tokens": 999999,
                    "completion_tokens": 999999,
                    "total_tokens": 1999998,
                },
                "llm_calls": [],
            }
        ),
        encoding="utf-8",
    )
    recorder.llm_call_started(
        model="test-model",
        iteration=1,
        span_id="llm-2",
        input_estimated_tokens=100,
        context_kind="sliding_window",
    )

    assert first["status"] == "ready"
    assert recorder.to_payload()["forecast"] == first
