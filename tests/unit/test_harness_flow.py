from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.command_logs import read_commands
from chatcopilot.harness.flow import project_flow, step_detail
from chatcopilot.harness.flow_records import record_step, step_binding
from chatcopilot.harness.models import Cancelled, HarnessError
from chatcopilot.harness.store import HarnessStore
from console.backend.routes.harness import router


@pytest.fixture
def store(tmp_path):
    store = HarnessStore(tmp_path / "harness")
    store.create({"task_id": "repair-example", "request_key": "r", "match_key": "m", "context_key": "c",
                  "active_key": "a", "source": {"kind": "robot_task", "run_id": "run-synthetic"},
                  "stage": "coding", "options": {}, "base_commit": "synthetic"})
    store.update("repair-example", status="running")
    return store


def test_step_input_is_persisted_before_execution_and_finish_does_not_rewrite_it(store):
    inputs = {"checks": ["image", "semantics"]}
    with record_step(store, "repair-example", "verify-1", "复测", group="attempt-1", inputs=inputs,
                     attempt=1) as result:
        task = store.get("repair-example")
        step = task["flow_steps"][0]
        assert step["status"] == "running" and step["input"] == inputs
        assert step_binding() == {"task_id": "repair-example", "flow_step_id": step["id"]}
        inputs["checks"].append("later")
        result.conclusion = "冻结检查通过"
    task = store.get("repair-example")
    assert task["flow_steps"][0]["input"]["checks"] == ["image", "semantics"]
    assert task["flow_steps"][0]["conclusion"] == "冻结检查通过"
    assert step_binding() == {}


def test_retry_has_its_own_record_and_cancelled_task_cannot_gain_success(store):
    for code in ("environment", "cancelled"):
        with pytest.raises(HarnessError):
            with record_step(store, "repair-example", "verify-1", "复测", group="attempt-1", inputs={}):
                if code == "cancelled":
                    store.update("repair-example", status="cancelled")
                    raise Cancelled()
                raise HarnessError(code, "synthetic failure")
    task = store.get("repair-example")
    assert len({s["id"] for s in task["flow_steps"]}) == 2
    assert [s["status"] for s in task["flow_steps"]] == ["failed", "cancelled"]
    last = task["flow_steps"][-1]
    store.save_flow_step("repair-example", {**last, "status": "completed"})
    assert store.get("repair-example")["flow_steps"][-1]["status"] == "cancelled"


def test_cancel_between_start_and_success_preserves_terminal_state(store):
    with record_step(store, "repair-example", "coding", "候选", group="attempt-1", inputs={}):
        store.update("repair-example", status="cancel_requested")
    task = store.get("repair-example")
    assert task["status"] == "cancel_requested"
    assert task["flow_steps"][0]["status"] == "cancelled"


@pytest.mark.parametrize("stop", ["cancelled", "blocked", "worker_interrupted"])
def test_resume_does_not_revive_unfinished_observations(store, stop):
    store.save_flow_step("repair-example", {"id": "old", "phase": "coding", "title": "旧调用", "status": "running",
        "parent_id": "attempt-1", "input": {}, "conclusion": "执行中", "locator": {}})
    if stop == "worker_interrupted":
        store.interrupt("repair-example")
    else:
        store.update("repair-example", status=stop)
    task = store.update("repair-example", status="queued")
    assert task["flow_steps"][0]["status"] in {"cancelled", "interrupted"}
    assert project_flow(task, [])["current_step_id"] is None
    assert project_flow(task, [])["default_step_id"] == "old"


def test_historical_versions_keep_failures_and_missing_input_without_fabricated_times(store):
    task = store.update("repair-example", status="blocked", preparation_revisions=[
        {"revision": 1, "status": "failed", "diagnosis": {"reason": "hypothesis one"},
         "review": {"decision": "rejected", "reason": "wrong target"}, "error": {"message": "revise draft"}},
        {"revision": 2, "attempt": 1, "status": "validated", "diagnosis": {"reason": "hypothesis two"}},
    ])
    before = copy.deepcopy(task)
    flow = project_flow(task, [{"number": 1, "status": "coding_failed", "error": "synthetic"}])
    assert next(g for g in flow["groups"] if g["id"] == "prepare-2")["parent_id"] == "attempt-1"
    old = step_detail(task, [], "prepare-1-diagnosis")
    assert old["input"] is None and old["result"]["reason"] == "hypothesis one"
    assert step_detail(task, [], "prepare-1-review")["conclusion"] == "wrong target"
    assert all(s["started_at"] is None for s in flow["steps"])
    assert task == before


def test_reproduction_failure_is_successful_reproduction_not_execution_error(store):
    task = store.update("repair-example", evaluations={
        "reproduce": {"complete": True, "passed_cases": [], "failed_cases": ["target"]},
        "verify-1": {"complete": True, "passed_cases": [], "failed_cases": ["target"]},
        "confirm-1": {"complete": True, "error": {"message": "missing image receipt"}},
    })
    rows = {r["phase"]: r for r in project_flow(task, [])["steps"]}
    assert rows["reproduce"]["status"] == "completed"
    assert "已复现" in rows["reproduce"]["conclusion"]
    assert rows["verify-1"]["status"] == "failed"
    assert rows["confirm-1"]["conclusion"] == "missing image receipt"


def test_execution_and_archive_availability_do_not_claim_repair_success(store):
    task = store.get("repair-example")
    attempts = [{"number": 1, "status": "verifying", "coding": {"events": [{"type": "command_execution", "command": "hidden command", "aggregated_output": "hidden output"}], "final_text": "AI says fixed"}}]
    flow = project_flow(task, attempts)
    assert "hidden command" not in json.dumps(flow)
    body = step_detail(task, attempts, "coding-1")
    assert "hidden command" not in json.dumps(body)
    assert body["result"]["final_text"] == "AI says fixed"
    assert "验收结论见复测" in body["conclusion"]
    task["source"]["original_input"] = {"command": "a user-supplied field, not an execution event"}
    assert step_detail(task, [], "source-evidence")["result"]["source"]["original_input"] == task["source"]["original_input"]


def test_trace_binding_uses_execution_id_and_does_not_guess_by_time(store):
    with record_step(store, "repair-example", "coding", "候选", group="attempt-1", inputs={},
                     locator={"section": "attempts", "number": 1, "field": "coding"}):
        ident = step_binding()["flow_step_id"]
    task = store.update("repair-example", trace_records={
        "bound": {"trace_ref": "bound", "source": {"flow_step_id": ident}, "capture_state": "available", "directory": "attempt-1/traces"},
        "other": {"trace_ref": "other", "source": {"flow_step_id": "different"}, "capture_state": "available", "directory": "attempt-1/traces"},
    })
    assert [t["trace_ref"] for t in step_detail(task, [], ident)["traces"]] == ["bound"]
    with pytest.raises(HarnessError, match="没有该流程"):
        step_detail(task, [], "other-task-step")


def test_step_detail_projects_context_metrics_without_private_source_index(store):
    artifacts = ArtifactRepository(store.root / "jobs/repair-example")
    output = artifacts.put("plan", 1, {"summary": "bounded plan"})
    execution = artifacts.put("execution", 1, {"context_metrics": {
        "prompt_bytes": 100, "source_index_bytes": 200, "command_count": 2,
        "context_budget_warning": ["plan_command_output"]}})
    with record_step(store, "repair-example", "plan", "计划", group="attempt-1", inputs={},
                     source_id="plan-1") as receipt:
        ident = step_binding()["flow_step_id"]
        receipt.evidence = {"output": output.__dict__, "execution": execution.__dict__}
    controller = HarnessController.__new__(HarnessController)
    controller.store = store
    detail = controller.flow("repair-example", step_id=ident)
    assert detail["context_metrics"]["source_index_bytes"] == 200
    assert "candidates" not in json.dumps(detail["context_metrics"])


def test_retry_detail_does_not_read_later_evaluation_result(store):
    with record_step(store, "repair-example", "verify-1", "复测", group="attempt-1", inputs={},
                     locator={"section": "evaluations", "key": "verify-1"}):
        ident = step_binding()["flow_step_id"]
    task = store.update("repair-example", evaluations={"verify-1": {"flow_step_id": "new", "complete": True}},
        evaluation_history={ident: {"flow_step_id": ident, "error": {"message": "original failure"}}})
    assert step_detail(task, [], ident)["result"]["error"]["message"] == "original failure"


def test_historical_attempt_only_checks_and_host_receipts_remain_visible(store):
    task = store.update("repair-example", commit_checks=[{"label": "架构检查", "exit_code": 0, "output": "private command output"}])
    attempts = [{"number": 1, "status": "accepted", "verification": {"complete": True, "passed_cases": ["target"]},
                 "confirmation": {"complete": True, "passed_cases": ["target"]}}]
    flow = project_flow(task, attempts)
    assert {"候选复测", "独立确认", "架构检查"}.issubset({r["title"] for r in flow["steps"]})
    assert "private command output" not in json.dumps(step_detail(task, attempts, "host-check-0"))


def test_delivery_receipts_survive_later_changes_without_duplication(store):
    with record_step(store, "repair-example", "review", "审核", group="attempt-1", inputs={}):
        pass
    first = {"state": "waiting_checks", "repository": "example/repo", "base_branch": "main"}
    store.update("repair-example", delivery=first)
    before = store.get("repair-example")["flow_steps"]
    store.update("repair-example", delivery={**first, "updated_at": 12})
    assert store.get("repair-example")["flow_steps"] == before
    task = store.update("repair-example", delivery={**first, "state": "checks_failed"})
    rows = [s for s in task["flow_steps"] if s["parent_id"] == "delivery"]
    assert len(rows) == 2
    assert step_detail(task, [], rows[0]["id"])["result"] == "waiting_checks"


def write_log(store, relative: str, items: list[dict]) -> Path:
    path = store.root / "jobs" / "repair-example" / relative / "public-events.jsonl"
    for parent in reversed(path.parents):
        if parent.is_relative_to(store.root) and not parent.exists():
            parent.mkdir(mode=0o700)
    path.write_bytes(b"".join(json.dumps(item).encode() + b"\n" for item in items))
    path.chmod(0o600)
    return path


def command(i, **extra):
    return {"type": "command_execution", "command": f"printf item-{i}", "exit_code": i % 2, **extra}


def test_commands_paginate_without_duplicates_and_ignore_messages(store):
    write_log(store, "attempt-1", [item for i in range(63) for item in ({"type": "agent_message", "text": "message"}, command(i))])
    task, attempts = store.get("repair-example"), [{"number": 1}]
    cursor, events = "", []
    while True:
        page = read_commands(store.root, task, attempts, source_id="coding-1", cursor=cursor)
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(events) == len({e["id"] for e in events}) == 63
    assert [e["command"] for e in events] == [f"printf item-{i}" for i in range(63)]


def test_commands_wait_for_partial_tail_and_can_read_later_append(store):
    path = write_log(store, "attempt-1", [command(0, exit_code=None)])
    with path.open("ab") as stream:
        stream.write(b'{"type":"command_execution",')
    args = (store.root, store.get("repair-example"), [{"number": 1}])
    first = read_commands(*args, source_id="coding-1")
    second = read_commands(*args, source_id="coding-1", cursor=first["next_cursor"])
    assert second["events"] == [] and not second["has_more"]
    assert first["events"][0]["exit_code"] is None
    with path.open("ab") as stream:
        stream.write(b'"command":"later","exit_code":0}\n')
    assert read_commands(*args, source_id="coding-1", cursor=second["next_cursor"])["events"][0]["command"] == "later"


def test_preparation_review_log_and_source_bound_cursor(store):
    task = store.update("repair-example", preparation_revisions=[{"revision": 1, "status": "running"}])
    write_log(store, "reproducer/revision-1/review", [command(1)])
    write_log(store, "attempt-1", [command(2)])
    page = read_commands(store.root, task, [{"number": 1}], source_id="prepare-review-1")
    assert page["events"][0]["command"].endswith("item-1")
    with pytest.raises(HarnessError, match="分页位置"):
        read_commands(store.root, task, [{"number": 1}], source_id="coding-1", cursor=page["next_cursor"])
    with pytest.raises(HarnessError):
        read_commands(store.root, task, [], source_id="../../other")


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions", "cursor"])
def test_commands_reject_unsafe_files_and_invalid_cursor(store, unsafe):
    path = write_log(store, "attempt-1", [command(1)])
    cursor = ""
    if unsafe == "symlink":
        moved = path.with_suffix(".moved")
        path.rename(moved)
        path.symlink_to(moved)
    elif unsafe == "hardlink":
        path.with_suffix(".link").hardlink_to(path)
    elif unsafe == "permissions":
        path.chmod(0o644)
    else:
        cursor = base64.urlsafe_b64encode(b"[]").decode()
    with pytest.raises(HarnessError):
        read_commands(store.root, store.get("repair-example"), [{"number": 1}], source_id="coding-1", cursor=cursor)


@pytest.mark.parametrize("endpoint,method,kwargs", [
    ("flow", "flow", {}), ("flow/steps/step-one", "flow", {"step_id": "step-one"}),
    ("commands?source_id=coding-1&cursor=abc", "commands", {"source_id": "coding-1", "cursor": "abc"}),
])
def test_flow_routes_are_read_only_and_no_store(endpoint, method, kwargs):
    app = FastAPI()
    app.include_router(router)
    read = Mock(return_value={"synthetic": True})
    app.state.harness = SimpleNamespace(**{method: read})
    with TestClient(app) as client:
        result = client.get("/api/harness/tasks/repair-example/" + endpoint)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    read.assert_called_once_with("repair-example", **kwargs)


def test_summary_does_not_ship_execution_bodies(store):
    controller = HarnessController.__new__(HarnessController)
    controller.get = lambda _: {**store.get("repair-example"), "evaluations": {"large": "body"},
        "attempts": [{"coding": {"events": [command(0)]}}], "source": {"kind": "robot_task", "diagnosis": {"large": "body"}}}
    result = controller.summary("repair-example")
    assert "attempts" not in result and "evaluations" not in result
    assert "diagnosis" not in result["source"]


def test_coder_archive_is_bound_to_the_active_business_step(store, monkeypatch, tmp_path):
    from chatcopilot.core.trace_capture import current_capture
    from chatcopilot.harness.codex_adapter import CodexCoder
    from chatcopilot.harness.models import RepairOptions
    coder = CodexCoder(lambda root, ref: store.register_trace("repair-example", root, ref))
    def execute(*args, **kwargs):
        current_capture().record({"kind": "coding_request"}, {"prompt": "synthetic input"})
        return {"final_text": json.dumps({"summary": "synthetic output", "notes": [], "needs_replan": False, "gaps": []})}
    monkeypatch.setattr(coder, "_execute_impl", execute)
    with record_step(store, "repair-example", "coding", "候选", group="attempt-1", inputs={}):
        ident = step_binding()["flow_step_id"]
        from chatcopilot.harness.agent_types import AgentCall, Role
        coder.execute(tmp_path, AgentCall("repair-example", Role.CODING, 1, "fixture", {"source": {}}), RepairOptions("synthetic"),
                       store.root / "jobs" / "repair-example" / "attempt-1", lambda: None)
    detail = step_detail(store.get("repair-example"), [], ident)
    assert len(detail["traces"]) == 1
    assert detail["traces"][0]["source"]["flow_step_id"] == ident


def test_commands_skip_oversized_line_explicitly_and_continue_paging(store, monkeypatch):
    import chatcopilot.harness.command_logs as logs
    monkeypatch.setattr(logs, "TAIL_BYTES", 200)
    path = write_log(store, "attempt-1", [])
    path.write_bytes(json.dumps(command(1, aggregated_output="x" * 650)).encode() + b"\n" + json.dumps(command(2)).encode() + b"\n")
    args = (store.root, store.get("repair-example"), [{"number": 1}])
    cursor, events, clipped = "", [], False
    for _ in range(10):
        page = read_commands(*args, source_id="coding-1", cursor=cursor)
        cursor = page["next_cursor"]
        events.extend(page["events"])
        clipped |= page["truncated"]
        if not page["has_more"]:
            break
    assert clipped and [e["command"] for e in events] == ["printf item-2"]


@pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError, FileNotFoundError])
def test_coder_execution_errors_keep_redacted_cause_in_environment_failure(tmp_path, monkeypatch, error_type):
    from chatcopilot.harness.codex_adapter import CodexCoder
    from chatcopilot.harness.agent_types import AgentCall, Role
    from chatcopilot.harness.models import RepairOptions
    secret = "fixture-only-startup-secret"
    monkeypatch.setenv("HARNESS_TEST_API_TOKEN", secret)
    failure = error_type("transport failed with " + secret)
    coder = CodexCoder()
    def execute(*_args):
        raise failure
    monkeypatch.setattr(coder, "_execute_impl", execute)
    output = tmp_path / "role"
    with pytest.raises(HarnessError) as caught:
        coder.execute(tmp_path, AgentCall("fixture", Role.MAIN, 1, "fixture", {"source": {"kind": "code_health"}}),
                      RepairOptions("fixture", timeout_seconds=None), output, lambda: None)
    assert caught.value.code == "coding_environment"
    assert caught.value.__cause__ is failure
    assert error_type.__name__ in str(caught.value) and "transport failed" in str(caught.value)
    assert secret not in str(caught.value)
    trace = next(output.glob("traces/*/trace.json")).read_text()
    assert "coding_environment" in trace and secret not in trace


@pytest.mark.parametrize("text", ["not JSON", None])
def test_coder_only_classifies_final_json_parse_errors_as_invalid_role(tmp_path, monkeypatch, text):
    from chatcopilot.harness.codex_adapter import CodexCoder
    from chatcopilot.harness.agent_types import AgentCall, Role
    from chatcopilot.harness.models import RepairOptions
    coder = CodexCoder()
    monkeypatch.setattr(coder, "_execute_impl", lambda *_: {"final_text": text})
    with pytest.raises(HarnessError) as caught:
        coder.execute(tmp_path, AgentCall("fixture", Role.MAIN, 1, "fixture", {"source": {}}),
                      RepairOptions("fixture"), tmp_path / "role", lambda: None)
    assert caught.value.code == "invalid_role_result"


@pytest.mark.parametrize("failure", [Cancelled(), HarnessError("budget_exhausted", "spent"),
    HarnessError("session_unconfirmed", "uncertain"), HarnessError("protected_change", "protected")])
def test_coder_keeps_host_control_errors_unchanged(tmp_path, monkeypatch, failure):
    from chatcopilot.harness.codex_adapter import CodexCoder
    from chatcopilot.harness.agent_types import AgentCall, Role
    from chatcopilot.harness.models import RepairOptions
    coder = CodexCoder()
    def execute(*_args):
        raise failure
    monkeypatch.setattr(coder, "_execute_impl", execute)
    with pytest.raises(HarnessError) as caught:
        coder.execute(tmp_path, AgentCall("fixture", Role.MAIN, 1, "fixture", {"source": {}}),
                      RepairOptions("fixture"), tmp_path / "role", lambda: None)
    assert caught.value is failure


def test_coder_does_not_reclassify_recorded_storage_errors(tmp_path, monkeypatch):
    from chatcopilot.harness.codex_adapter import CodexCoder
    from chatcopilot.harness.agent_types import AgentCall, Role
    from chatcopilot.harness.models import RepairOptions
    failure = OSError("storage interrupted")
    failure.storage_details = {"database": "fixture", "phase": "commit"}
    coder = CodexCoder()
    def execute(*_args):
        raise failure
    monkeypatch.setattr(coder, "_execute_impl", execute)
    with pytest.raises(OSError) as caught:
        coder.execute(tmp_path, AgentCall("fixture", Role.MAIN, 1, "fixture", {"source": {}}),
                      RepairOptions("fixture"), tmp_path / "role", lambda: None)
    assert caught.value is failure
