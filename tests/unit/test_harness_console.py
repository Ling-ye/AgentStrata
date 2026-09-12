from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.backend.routes.harness import router


@pytest.fixture
def app():
    value = FastAPI()
    value.include_router(router)
    value.state.harness = SimpleNamespace(
        start_case_instance=Mock(return_value={"task_id": "repair-example", "status": "queued"}),
        start_task=Mock(return_value={"task_id": "repair-robot", "status": "queued"}),
        load_source=Mock(return_value={"kind": "robot_task", "blockers": [], "history": []}),
        list=Mock(return_value={"tasks": [], "total": 0}),
        get=Mock(return_value={"status": "fixed", "uncommitted": True}),
        cancel=Mock(return_value={"status": "cancelled"}),
        resume=Mock(return_value={"status": "queued"}),
        patch=Mock(return_value=b"diff --git a/example.py b/example.py\n"),
    )
    return value


def body():
    return {
        "case_instance_id": "case-" + "a" * 32,
        "request_id": "stable-request",
        "model": "test-model",
    }


def test_local_start_uses_public_controller(app):
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        response = client.post("/api/harness/tasks", json=body())
    assert response.status_code == 200
    assert app.state.harness.start_case_instance.call_args.args[0] == body()["case_instance_id"]
    assert app.state.harness.start_case_instance.call_args.kwargs["request_id"] == "stable-request"


def test_remote_and_cross_origin_writes_do_not_start_worker(app):
    with TestClient(app, client=("192.0.2.5", 41000)) as client:
        assert client.post("/api/harness/tasks", json=body()).status_code == 403
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        assert (
            client.post(
                "/api/harness/tasks", json=body(), headers={"Origin": "https://example.org"}
            ).status_code
            == 403
        )
    app.state.harness.start_case_instance.assert_not_called()


def test_independent_page_loads_sources_and_lists_history(app):
    with TestClient(app) as client:
        response = client.post(
            "/api/harness/sources/load",
            json={"kind": "robot_task", "source_id": "run-example", "bot_id": "sample"},
        )
        assert response.status_code == 200
        assert (
            client.get("/api/harness/tasks?page=2&search=run-example&status=blocked").status_code
            == 200
        )
    app.state.harness.load_source.assert_called_once_with("robot_task", "run-example", "sample")
    app.state.harness.list.assert_called_once_with(
        page=2, limit=20, search="run-example", status="blocked"
    )


def test_robot_task_creation_never_uses_console_maintenance_task_ids(app):
    payload = {
        "source_kind": "robot_task",
        "bot_id": "sample",
        "run_id": "run-example",
        "request_id": "robot-request",
        "model": "test-model",
    }
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        assert client.post("/api/harness/tasks", json=payload).status_code == 200
        assert (
            client.post(
                "/api/harness/tasks", json={**payload, "evaluation_id": "eval-source"}
            ).status_code
            == 422
        )
        assert client.post("/api/harness/tasks", json={**payload, "bot_id": ""}).status_code == 422
        assert (
            client.post(
                "/api/harness/tasks", json={**payload, "reasoning_effort": "unknown"}
            ).status_code
            == 400
        )
    app.state.harness.start_task.assert_called_once()
    app.state.harness.start_case_instance.assert_not_called()


def test_patch_download_is_separate_from_eval_results(app):
    with TestClient(app) as client:
        patch = client.get("/api/harness/tasks/repair-example/attempts/1/patch")
        assert patch.status_code == 200 and "attachment" in patch.headers["content-disposition"]
    app.state.harness.patch.assert_called_once_with("repair-example", 1)


@pytest.mark.parametrize("feedback", [None, {}, {"repair_hint": "检查分词"},
    {"expected_behavior": "保留换行"}, {"repair_hint": "检查分词", "expected_behavior": "保留换行"}])
def test_feedback_reaches_controller_and_only_case_expectation_override_is_rejected(app, feedback):
    payload = {"source_kind": "robot_task", "bot_id": "sample", "run_id": "run-example",
               "request_id": "robot-feedback", "model": "test-model", "feedback": feedback}
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        assert client.post("/api/harness/tasks", json=payload).status_code == 200
        evaluation = client.post("/api/harness/tasks", json={**body(), "feedback": feedback})
        assert evaluation.status_code == (422 if feedback and feedback.get("expected_behavior") else 200)
    supplied = app.state.harness.start_task.call_args.kwargs["feedback"]
    assert (supplied.to_payload() if supplied else {}) == (feedback or {})


@pytest.mark.parametrize("feedback", ["answer", {"expected_behavior": 42},
    {"repair_hint": None}, {"expected_behavior": ["answer"]}, {"unknown": "value"}])
def test_invalid_feedback_is_rejected_before_dispatch(app, feedback):
    payload = {"source_kind": "robot_task", "bot_id": "sample", "run_id": "run-example",
               "request_id": "robot-feedback", "model": "test-model", "feedback": feedback}
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        assert client.post("/api/harness/tasks", json=payload).status_code == 422
    app.state.harness.start_task.assert_not_called()


def test_start_task_cli_passes_feedback_without_changing_commit_option(monkeypatch, capsys):
    from chatcopilot.harness import __main__ as cli

    controller = Mock()
    controller.start_task.return_value = {"task_id": "repair-example"}
    monkeypatch.setattr(cli, "HarnessController", Mock(return_value=controller))
    assert cli.main(["start-task", "--bot", "sample", "--run", "run-example",
                     "--gateway-state-root", "synthetic-state", "--model", "test-model",
                     "--repair-hint", "检查分词", "--expected-behavior", "保留换行\n及空格"]) == 0
    kwargs = controller.start_task.call_args.kwargs
    assert kwargs["feedback"].to_payload() == {"repair_hint": "检查分词", "expected_behavior": "保留换行\n及空格"}
    assert kwargs["review_and_commit"] is False
    assert "repair-example" in capsys.readouterr().out


def test_frontend_cannot_supply_host_paths_or_status(app):
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        assert (
            client.post(
                "/api/harness/tasks", json={**body(), "worktree": "/unknown", "status": "fixed"}
            ).status_code
            == 422
        )
    app.state.harness.start_case_instance.assert_not_called()


def test_review_commit_is_explicit_and_boolean(app):
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        assert client.post("/api/harness/tasks", json=body()).status_code == 200
        assert app.state.harness.start_case_instance.call_args.kwargs["review_and_commit"] is False
        assert (
            client.post(
                "/api/harness/tasks", json={**body(), "review_and_commit": True}
            ).status_code
            == 200
        )
        assert app.state.harness.start_case_instance.call_args.kwargs["review_and_commit"] is True
        assert (
            client.post(
                "/api/harness/tasks", json={**body(), "review_and_commit": "true"}
            ).status_code
            == 422
        )


@pytest.mark.parametrize("field", ["evaluation_id", "case_ref", "target_id"])
def test_case_instance_creation_rejects_client_routing_fields(app, field):
    with TestClient(app, client=("127.0.0.1", 41000)) as client:
        assert client.post("/api/harness/tasks", json={**body(), field: "another"}).status_code == 422
    app.state.harness.start_case_instance.assert_not_called()
