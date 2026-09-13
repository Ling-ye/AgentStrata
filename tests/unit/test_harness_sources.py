from __future__ import annotations

import copy
import hashlib
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
from chatcopilot.harness.gateway_adapter import task_source
from chatcopilot.harness.local_verifier import LocalVerifier
from chatcopilot.harness.models import HarnessError, RepairFeedback, RepairOptions
from test_case_harness import run_task

ROOT = Path(__file__).resolve().parents[2]


def robot_source(*, blockers=()):
    return {
        "kind": "robot_task",
        "bot_id": "sample",
        "run_id": "run-example",
        "revision": "evidence-revision",
        "evidence": {"request": "expected behavior"},
        "blockers": list(blockers),
        "failure_signature": [{"error": "wrong_result"}],
        "case_id": "reproduction",
        "target_id": "local-pytest",
        "case_ids": ["reproduction"],
        "passed_cases": [],
        "repetitions": 1,
    }


@pytest.mark.parametrize("feedback", [
    None, RepairFeedback(), RepairFeedback(" \n", "\t"),
    RepairFeedback(repair_hint="检查输入处理\n保留换行"),
    RepairFeedback(expected_behavior="保留换行"),
    RepairFeedback("检查输入处理", "保留换行"),
])
def test_feedback_is_saved_separately_and_read_after_restart(tmp_path, feedback):
    original = robot_source()
    before = copy.deepcopy(original)
    root = tmp_path / "private"
    controller = HarnessController(ROOT, root=root, task_reader=lambda *_: original)
    task = controller.start_task(
        "sample", "run-example", RepairOptions("test-model"), feedback=feedback, launch=False
    )
    expected = feedback.to_payload() if feedback else {}
    reopened = HarnessController(ROOT, root=root)
    assert reopened.get(task["task_id"])["source"].get("feedback", {}) == expected
    assert reopened.evidence(task["task_id"]).get("feedback", {}) == expected
    assert reopened.evidence(task["task_id"])["evidence"] == before["evidence"]
    assert original == before
    assert reopened.store.get(task["task_id"])["source"]["revision"] == before["revision"]


def test_feedback_changes_request_and_candidate_identity_but_keeps_source_history(tmp_path, monkeypatch):
    controller = HarnessController(ROOT, root=tmp_path / "private", task_reader=lambda *_: robot_source())
    args = ("sample", "run-example", RepairOptions("test-model"))
    feedback = RepairFeedback("检查输入", "保留换行")
    first = controller.start_task(*args, feedback=feedback, request_id="original", launch=False)
    same = controller.start_task(*args, feedback=feedback, request_id="original", launch=False)
    active = controller.start_task(*args, feedback=feedback, request_id="duplicate", launch=False)
    assert first["task_id"] == same["task_id"] == active["task_id"]
    controller.store.update(first["task_id"], status="fixed")
    monkeypatch.setattr(controller, "_candidate_available", lambda _: True)
    reused = controller.start_task(*args, feedback=feedback, request_id="reuse", launch=False)
    assert reused["task_id"] == first["task_id"] and reused["reused"]
    for index, changed in enumerate([
        RepairFeedback("检查分词", "保留换行"), RepairFeedback("检查输入", "保留空格"), None,
    ]):
        with pytest.raises(HarnessError, match="内容已变化"):
            controller.start_task(*args, feedback=changed, request_id="original", launch=False)
        fresh = controller.start_task(*args, feedback=changed, request_id=f"changed-{index}", launch=False)
        assert fresh["task_id"] != first["task_id"] and not fresh.get("reused")
    assert len(controller.load_source("robot_task", "run-example", "sample")["history"]) == 4
    assert controller.get(first["task_id"])["source"]["feedback"] == feedback.to_payload()


def test_empty_feedback_uses_same_identity_as_omitted_feedback(tmp_path):
    controller = HarnessController(ROOT, root=tmp_path / "private", task_reader=lambda *_: robot_source())
    args = ("sample", "run-example", RepairOptions("test-model"))
    first = controller.start_task(*args, request_id="same", launch=False)
    again = controller.start_task(*args, feedback=RepairFeedback("\n", " "), request_id="same", launch=False)
    assert again["task_id"] == first["task_id"]
    assert "feedback" not in controller.store.get(first["task_id"])["source"]


def test_source_previews_group_failed_repetitions_and_include_target():
    trials = [
        {"case_ref": "suite:case", "case_id": "case", "target_id": target, "outcome": outcome,
         "error": {"stage": "execution", "code": "execution_error", "message": "controlled failure"} if outcome == "error" else None}
        for target, outcome in [
            ("first", "failed"),
            ("first", "failed"),
            ("second", "error"),
            ("third", "passed"),
        ]
    ]
    client = Mock()
    client.get.return_value = {
        "status": "completed",
        "conditions": {"complete": True},
        "request": {"kind": "suite"},
        "result": {"trials": trials},
    }
    preview = ServiceEvaluator(client).load("eval-sample")
    assert not preview["blockers"]
    assert [item["target_id"] for item in preview["failures"]] == ["first", "second"]


def test_case_instance_load_and_source_resolve_on_server_and_keep_original_attempt():
    identifier = "case-" + "a" * 32
    trial = {"trial_id": "trial-b-2", "case_id": "b", "case_ref": "suite:b", "target_id": "main", "attempt": 2, "outcome": "failed"}
    client = Mock()
    client.case_instance.return_value = {
        "case_instance_id": identifier, "evaluation_id": "eval-source", "case_ref": "suite:b",
        "target_id": "main", "attempt": 2, "trial_id": "trial-b-2", "trial": trial,
    }
    client.get.return_value = {
        "status": "completed", "conditions": {"complete": True},
        "request": {"kind": "suite"},
        "result": {"trials": [trial], "targets": [{"target_id": "main", "executor": "agent_configured"}]},
    }
    evaluator = ServiceEvaluator(client)
    preview = evaluator.load_instance(identifier)
    assert any("完整本地执行归档" in item for item in preview["blockers"]) and "failures" not in preview
    assert preview["case_instance"]["attempt"] == 2
    source = {"trials": [trial], "repetitions": 3, "passed_cases": ["a"]}
    evaluator.source = Mock(return_value=source)
    resolved = evaluator.source_instance(identifier)
    evaluator.source.assert_called_once_with("eval-source", "suite:b", "main", include_trace=False)
    assert resolved["case_instance_id"] == identifier and resolved["trial_id"] == "trial-b-2"
    assert resolved["repetitions"] == 3 and resolved["passed_cases"] == ["a"]
    client.case_instance.return_value["trial"] = {**trial, "outcome": "passed"}
    assert evaluator.load_instance(identifier)["blockers"]
    with pytest.raises(HarnessError, match="没有失败"):
        evaluator.source_instance(identifier)
    assert evaluator.source.call_count == 1


def test_mismatched_case_instance_never_resolves_to_a_different_source():
    client = Mock()
    evaluator = ServiceEvaluator(client)
    client.case_instance.return_value = {"case_instance_id": "case-" + "b" * 32}
    with pytest.raises(HarnessError, match="不一致"):
        evaluator.source_instance("case-" + "a" * 32)
    client.get.assert_not_called()


def test_task_selfcheck_is_persisted_and_never_dispatches_with_missing_evidence(
    tmp_path, monkeypatch
):
    reader = Mock(return_value=robot_source(blockers=["缺少实际输入记录"]))
    controller = HarnessController(ROOT, root=tmp_path / "private", task_reader=reader)
    launch = Mock()
    monkeypatch.setattr(controller, "_launch", launch)
    result = controller.start_task(
        "sample", "run-example", RepairOptions("test-model"), request_id="one",
        feedback=RepairFeedback(expected_behavior="参考答案不能补全观测"),
    )
    assert result["status"] == "blocked" and result["stage"] == "self_check"
    launch.assert_not_called()
    reader.assert_called_once_with("sample", "run-example")
    reader.side_effect = RuntimeError("offline")
    again = controller.start_task(
        "sample", "run-example", RepairOptions("test-model"), request_id="one",
        feedback=RepairFeedback(expected_behavior="参考答案不能补全观测"),
    )
    assert again["task_id"] == result["task_id"]
    assert controller.list()["tasks"][0]["task_id"] == result["task_id"]
    with pytest.raises(HarnessError, match="来源证据不完整"):
        controller.resume(result["task_id"])


def test_history_pagination_search_and_bot_binding(tmp_path):
    def reader(bot, run):
        return {**robot_source(), "bot_id": bot, "run_id": run}

    controller = HarnessController(ROOT, root=tmp_path / "private", task_reader=reader)
    for index in range(23):
        controller.start_task("sample", f"run-{index}", RepairOptions("test-model"), launch=False)
    first, second = controller.list(), controller.list(page=2)
    assert first["total"] == 23 and len(first["tasks"]) == 20 and len(second["tasks"]) == 3
    assert not (
        {task["task_id"] for task in first["tasks"]} & {task["task_id"] for task in second["tasks"]}
    )
    assert controller.list(search="run-22")["total"] == 1
    controller.start_task("another", "run-22", RepairOptions("test-model"), launch=False)
    preview = controller.load_source("robot_task", "run-22", "sample")
    assert len(preview["history"]) == 1 and preview["history"][0]["source"]["bot_id"] == "sample"


def test_gateway_adapter_paginates_and_does_not_read_business_state(monkeypatch):
    from chatcopilot.harness import gateway_adapter

    record = {
        "run": {
            "state": "failed",
            "run_id": "run-example",
            "input_ref": "input",
            "config_id": "cfg",
        },
        "observations": [{"seq": 1, "body_ref": "input"}],
        "has_more": True,
        "next_cursor": 1,
        "receipts": [],
        "outbox": [],
        "approvals": [],
    }
    reader = SimpleNamespace(
        meta=Mock(return_value=None),
        body=Mock(return_value={"state": "available", "payload": {"text": "example"}}),
        configuration=Mock(return_value={"revision": "cfg"}),
    )
    monkeypatch.setattr(gateway_adapter, "detail", Mock(return_value=record))
    page = Mock(
        return_value={
            "observations": [{"seq": 2, "body_ref": None}],
            "has_more": False,
            "next_cursor": 2,
        }
    )
    monkeypatch.setattr(gateway_adapter, "events", page)
    source = task_source(reader, "sample", "run-example")
    assert source["blockers"] == ["缺少本地执行记录，无法自动修复"]
    assert len(source["evidence"]["observations"]) == 2
    page.assert_called_once_with(reader, "run-example", after=1, limit=500)
    reader.body.assert_called_once_with("run-example", "input")
    changed = copy.deepcopy(record)
    changed["run"]["state"] = "running"
    changed["run"]["details_expired"] = True
    monkeypatch.setattr(gateway_adapter, "detail", Mock(return_value=changed))
    assert len(task_source(reader, "sample", "run-example")["blockers"]) == 3


@pytest.fixture(scope="module")
def repository(tmp_path_factory):
    root = tmp_path_factory.mktemp("harness-task-source") / "source"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(ROOT), str(root)], check=True)
    return root


class LocalFixture:
    def prepare(self, task, worktree, coder, options, check_cancel):
        return {
            **task["source"],
            "test_sha256": "frozen-test",
            "case_ids": ["reproduction", "protected", "old_failure"],
        }

    def run(self, task, worktree, evaluation_id, case_ids, check_cancel):
        check_cancel()
        file = worktree / "src/chatcopilot/core/harness_probe.py"
        text = file.read_text() if file.exists() else ""
        passing = {"protected"} if "regression" not in text else set()
        if "fixed" in text:
            passing.add("reproduction")
        return {
            "result": {
                "trials": [
                    {
                        "case_id": case,
                        "target_id": "local-pytest",
                        "attempt": 1,
                        "outcome": "passed" if case in passing else "failed",
                    }
                    for case in case_ids
                ]
            },
            "test_sha256": "frozen-test",
        }


@pytest.mark.parametrize("regression", [False, True])
def test_daily_task_fix_is_gated_by_target_and_previously_passing_tests(
    repository, tmp_path, regression
):
    controller = HarnessController(
        repository, root=tmp_path / "private", task_reader=lambda *_: robot_source()
    )
    task = controller.start_task(
        "sample", "run-example", RepairOptions("test-model", max_attempts=1), launch=False
    )

    def code(worktree, *_):
        (worktree / "src/chatcopilot/core/harness_probe.py").write_text(
            "VALUE = 'fixed " + ("regression" if regression else "") + "'\n"
        )
        return {}

    coder = SimpleNamespace(run=code)
    evaluator = Mock()
    result = run_task(
        controller.store, task["task_id"], evaluator, coder, local_verifier=LocalFixture()
    )
    assert result["status"] == ("failed" if regression else "fixed")
    assert result["protected_cases"] == ["protected"]
    assert result["current_evaluation_id"] is None
    assert result["evaluations"]["verify-1"]["test_sha256"] == "frozen-test"
    evaluator.run.assert_not_called()
    assert not evaluator.cancel.called


@pytest.fixture
def local_test(tmp_path):
    root = private_directory(tmp_path / "private")
    worktree = private_directory(tmp_path / "source")
    from chatcopilot.core.source_snapshot import git_output
    git_output(worktree, "init", "--quiet")
    (worktree / "tests/unit").mkdir(parents=True)
    task = {"task_id": "repair-test"}
    private_directory(root / "jobs" / task["task_id"] / "reproducer")
    private_directory(root / "jobs" / task["task_id"] / "reproducer" / "frozen")
    return LocalVerifier(root), task, worktree


def test_actual_isolated_pytest_reports_failure_and_preserves_source(local_test, monkeypatch):
    verifier, task, worktree = local_test
    monkeypatch.setenv("HARNESS_FIXTURE_SECRET", "must-stay-outside")
    test = worktree / "tests/unit/test_sample.py"
    content = """import os
from pathlib import Path
import pytest
def test_scope():
    assert "HARNESS_FIXTURE_SECRET" not in os.environ
    with pytest.raises(OSError):
        Path(__file__).write_text("changed")
def test_behavior():
    assert 1 == 2
"""
    test.write_text(content)
    result = verifier._pytest(task, worktree, ["tests/unit"], lambda: None)
    assert result["rows"]["tests/unit/test_sample.py::test_scope"]["outcome"] == "passed"
    failure = result["rows"]["tests/unit/test_sample.py::test_behavior"]
    assert failure["outcome"] == "failed" and failure["assertion_failure"]
    assert test.read_text() == content


@pytest.mark.parametrize(
    "outcome, assertion", [("error", False), ("skipped", False), ("failed", False)]
)
def test_environment_errors_and_skips_cannot_be_reproductions(
    local_test, monkeypatch, outcome, assertion
):
    verifier, task, worktree = local_test
    frozen = verifier.root / "jobs" / task["task_id"] / "reproducer/frozen/test_reproduction.py"
    frozen.write_text("def test_repro(): assert False\n")
    frozen.chmod(0o600)
    task["source"] = {
        **robot_source(),
        "test_path": str(frozen),
        "test_sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
        "test_nodeid": "test_repro",
    }
    monkeypatch.setattr(
        verifier,
        "_pytest",
        lambda *_, **__: {
            "rows": {"test_repro": {"outcome": outcome, "assertion_failure": assertion}}
        },
    )
    with pytest.raises(HarnessError, match="行为断言"):
        verifier.run(task, worktree, "verification", ["reproduction"], lambda: None)
    frozen.write_text("def test_repro(): assert True\n")
    with pytest.raises(HarnessError, match="测试已变化"):
        verifier.run(task, worktree, "verification", ["reproduction"], lambda: None)


def test_local_test_cancellation_terminates_the_child(local_test):
    verifier, task, worktree = local_test
    (worktree / "tests/unit/test_wait.py").write_text(
        "import time\ndef test_wait(): time.sleep(120)\n"
    )
    started = time.monotonic()

    def cancel():
        if time.monotonic() - started > 1:
            raise HarnessError("cancelled", "cancelled")

    with pytest.raises(HarnessError, match="cancelled"):
        verifier._pytest(task, worktree, ["tests/unit"], cancel)
    assert time.monotonic() - started < 10


def test_evaluation_frontend_has_no_harness_imports_or_requests():
    files = list((ROOT / "console/web/src/features/evals").glob("*.ts*"))
    files.append(ROOT / "console/web/src/pages/EvalsPage.tsx")
    assert all("harness" not in file.read_text().lower() for file in files)


def test_generated_test_is_frozen_and_reused_with_real_candidate_import(local_test):
    import json

    verifier, task, worktree = local_test
    subprocess.run(["git", "init", "--quiet", str(worktree)], check=True)
    (worktree / "src").mkdir()
    product = worktree / "src/probe.py"
    product.write_text("VALUE = 0\n")
    (worktree / "tests/unit/test_existing.py").write_text(
        "from probe import VALUE\ndef test_existing(): assert VALUE >= 0\n"
    )
    task["source"] = robot_source()
    feedback = RepairFeedback("检查数值", "VALUE equals one").to_payload()
    task["source"]["feedback"] = feedback
    test_bytes = b"from probe import VALUE\ndef test_repro(): assert VALUE == 1\n"

    def prepare(_worktree, _evidence, _options, output, _check):
        assert _evidence["source"]["feedback"] == feedback
        assert _evidence["source"]["evidence"] == robot_source()["evidence"]
        draft = private_directory(output / "draft")
        (draft / "test_reproduction.py").write_bytes(test_bytes)
        (draft / "diagnosis.json").write_text(
            json.dumps(
                {
                    "reproducible": True,
                    "reason": "controlled test fixture",
                    "expected_behavior": "VALUE equals one",
                }
            )
        )
        for file in draft.iterdir():
            file.chmod(0o600)
        return {}

    task["source"] = verifier.prepare(
        task, worktree, SimpleNamespace(prepare=prepare), RepairOptions("test-model"), lambda: None
    )
    first = verifier.run(task, worktree, "reproduce", ["reproduction"], lambda: None)
    assert first["result"]["trials"][0]["outcome"] == "failed"
    product.write_text("VALUE = 1\n")
    second = verifier.run(task, worktree, "verify", task["source"]["case_ids"], lambda: None)
    assert {row["outcome"] for row in second["result"]["trials"]} == {"passed"}
    assert len(second["result"]["trials"]) == 2
    assert first["test_sha256"] == second["test_sha256"] == hashlib.sha256(test_bytes).hexdigest()
    assert first["code_source"]["sha256"] != second["code_source"]["sha256"]
    assert task["source"]["feedback"] == feedback


def test_reference_answer_cannot_replace_reproduction(repository, tmp_path):
    import json

    feedback = RepairFeedback(expected_behavior="给出需要外部来源确认的解释")
    controller = HarnessController(repository, root=tmp_path / "private", task_reader=lambda *_: robot_source())
    task = controller.start_task(
        "sample", "run-example", RepairOptions("test-model"), feedback=feedback, launch=False
    )

    def prepare(_worktree, evidence, _options, output, _check):
        assert evidence["source"]["feedback"] == feedback.to_payload()
        draft = private_directory(output / "draft")
        file = draft / "diagnosis.json"
        file.write_text(json.dumps({"reproducible": False, "reason": "需要真实模型与外部搜索验证"}))
        file.chmod(0o600)
        return {}

    coder = SimpleNamespace(prepare=prepare, run=Mock())
    evaluator = Mock()
    result = run_task(controller.store, task["task_id"], evaluator, coder)
    assert result["status"] == "blocked" and result["error_code"] == "not_reproducible"
    assert "需要真实模型" in result["message"]
    assert "verified_digest" not in result and "test_sha256" not in result["source"]
    coder.run.assert_not_called()
    evaluator.run.assert_not_called()


def test_gateway_task_evidence_reads_real_observation_database_without_writes(tmp_path):
    from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
    from chatcopilot.gateway.observation_runtime import ObservationRecorder
    from chatcopilot.gateway.observation_store import ObservationStore
    from chatcopilot.gateway.state_store import GatewayStateStore

    state = GatewayStateStore(tmp_path / "gateway")
    generation = state.acquire_writer_generation()
    recorder = ObservationRecorder(
        state, generation, configuration={"layers": [], "entities": [], "backend": "native"}
    )
    state.create_session(
        generation=generation,
        session_id="session-source",
        account=ChannelAccountRef("fixture", "account"),
        conversation=ConversationRef("p2p", "source"),
    )
    state.begin_run(
        generation=generation,
        session_id="session-source",
        run_id="run-source",
        input_fingerprint="a" * 64,
    )
    recorder.store.bind_run(
        "run-source",
        config_id=recorder.config_id,
        backend="native",
        model="test-model",
        role="owner",
    )
    recorder.store.attach_body("run-source", "input", {"text": "Return the requested value"})
    recorder.accepted("run-source", "Return the requested value", "owner")
    state.start_run(generation=generation, session_id="session-source", run_id="run-source")
    state.finish_run(
        generation=generation,
        session_id="session-source",
        run_id="run-source",
        outcome="failed",
        error_code="fixture_error",
        result={"final_text": "Wrong value"},
    )
    before = {
        str(file.relative_to(state.root)): file.read_bytes()
        for file in state.root.rglob("*")
        if file.is_file()
    }
    source = task_source(ObservationStore(state.root), "sample", "run-source")
    assert not source["blockers"]
    assert source["evidence"]["run"]["state"] == "failed"
    assert source["trace_bundle"]["trace"]["metadata"]["agentstrata"]["capture_state"] == "available"
    from chatcopilot.core.trace_archive import TraceArchive
    events = list(TraceArchive(recorder.store.root / "traces").portable_events(
        source["trace_bundle"]["trace"]["uuid"]))
    assert any(event["kind"] == "execution_result" and event["body"]["final_text"] == "Wrong value" for event in events)
    after = {
        str(file.relative_to(state.root)): file.read_bytes()
        for file in state.root.rglob("*")
        if file.is_file()
    }
    assert before == after
