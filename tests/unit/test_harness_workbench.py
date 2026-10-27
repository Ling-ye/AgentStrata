"""Console and CLI use the same goal and activation controls, with read-only projections."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console.backend.routes.harness import router
from chatcopilot.harness.workbench import applications, comparison


@pytest.fixture
def app():
    value = FastAPI()
    value.include_router(router)
    value.state.harness = SimpleNamespace(settings={}, **{name: Mock(return_value={"task_id": "repair-goal"}) for name in
        ("applications", "preview_improvement", "start_improvement", "comparison", "activation", "prepare_activation",
         "activate", "rollback_activation", "reconcile_activation", "cancel_activation")})
    return value


def goal():
    return {"task_type": "integrate", "bot_id": "fixture", "goal": "调用动作工具并确认执行",
            "allowed_paths": ["src/chatcopilot/agent"], "request_id": "request-one", "profile": "worker"}


def test_goal_api_preserves_scope_identity_and_independent_worker_configuration(app):
    with TestClient(app, client=("127.0.0.1", 40000)) as client:
        assert client.post("/api/harness/improvements", json=goal()).status_code == 200
    request, options = app.state.harness.start_improvement.call_args.args
    assert request.bot_id == "fixture" and request.task_type == "integrate"
    assert request.allowed_paths == ("src/chatcopilot/agent",)
    assert request.source_envelope == {} and request.goal_case_ids == ()
    assert options.model_profile == "worker" and options.model_settings
    assert app.state.harness.start_improvement.call_args.kwargs == {"request_id": "request-one"}


@pytest.mark.parametrize("updates", [{"allowed_paths": ["../outside"]}, {"allowed_paths": []},
    {"goal": " "}, {"source_envelope": {"source_url": "https://github.com/acme/project"}},
    {"runtime_id": "codex"}, {"allowed_paths": ["src", "src"]}])
def test_invalid_goal_does_not_dispatch(app, updates):
    with TestClient(app, client=("127.0.0.1", 40000)) as client:
        assert client.post("/api/harness/improvements", json={**goal(), **updates}).status_code in {400, 422}
    app.state.harness.start_improvement.assert_not_called()


@pytest.mark.parametrize("action,method", [("prepare", "prepare_activation"), ("activate", "activate"),
    ("rollback", "rollback_activation"), ("reconcile", "reconcile_activation"), ("cancel", "cancel_activation")])
def test_activation_api_delegates_to_existing_transaction(app, action, method):
    with TestClient(app, client=("127.0.0.1", 40000)) as client:
        assert client.post(f"/api/harness/tasks/repair-goal/activation/{action}", json={"target_id": "plugin:vtuber:main"}).status_code == 200
    expected = {"target_id": "plugin:vtuber:main", "timeout": 60} if action in {"prepare", "activate"} else {}
    getattr(app.state.harness, method).assert_called_once_with("repair-goal", **expected)


@pytest.mark.parametrize("path", ["/improvements", *[f"/tasks/repair-goal/activation/{action}" for action in
    ("prepare", "activate", "rollback", "reconcile", "cancel")]])
def test_all_new_mutations_require_local_same_origin(app, path):
    body = goal() if path == "/improvements" else {}
    with TestClient(app, client=("192.0.2.5", 40000)) as client:
        assert client.post("/api/harness" + path, json=body).status_code == 403
    with TestClient(app, client=("127.0.0.1", 40000)) as client:
        assert client.post("/api/harness" + path, json=body, headers={"Origin": "https://example.org"}).status_code == 403
    for name, method in vars(app.state.harness).items():
        if isinstance(method, Mock):
            method.assert_not_called()


def test_queries_only_read_and_do_not_prepare_or_reconcile(app):
    with TestClient(app) as client:
        assert client.get("/api/harness/applications").status_code == 200
        assert client.get("/api/harness/applications/fixture/preview?suite_id=suite").status_code == 200
        assert client.get("/api/harness/tasks/repair-goal/comparison").status_code == 200
        assert client.get("/api/harness/tasks/repair-goal/activation").status_code == 200
    app.state.harness.preview_improvement.assert_called_once_with("fixture", "suite")
    for method in ("start_improvement", "prepare_activation", "activate", "reconcile_activation"):
        getattr(app.state.harness, method).assert_not_called()


def test_application_catalog_uses_profile_without_exposing_credentials(tmp_path):
    for ident, runtime in (("live2d", "langgraph"), ("qq", "codex")):
        folder = tmp_path / "bots" / ident
        folder.mkdir(parents=True)
        (folder / "bot.yaml").write_text(f"id: {ident}\nagent_profile: agent.yaml\nchannels: {{}}\n")
        (folder / "agent.yaml").write_text(f"agents:\n  runtime: {runtime}\nllm:\n  private: do-not-project\n")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.yaml")}
    result = applications(tmp_path)
    assert [(item["bot_id"], item["runtime_id"]) for item in result["applications"]] == [("live2d", "langgraph"), ("qq", "codex")]
    assert "do-not-project" not in str(result)
    assert {path: path.read_bytes() for path in before} == before


def test_comparison_uses_the_accepted_attempt_and_retains_missing_evidence():
    task = {"source": {"kind": "application", "goal": "new action", "runtime_id": "langgraph"},
            "accepted_candidate": {"attempt": 1, "candidate_digest": "accepted"}, "verification_gaps": []}
    first = {"number": 1, "candidate_digest": "accepted", "reproduction": {"checks": [{"check_id": "action", "repetition": 1, "outcome": "failed", "failure_kind": "product"}]}}
    records = [first, {"number": 2, "candidate_digest": "unreviewed"}]
    before = deepcopy((task, records))
    store = SimpleNamespace(get=lambda _: task, attempts=lambda _: records)
    value = comparison(store, "repair-goal")
    assert value["candidate_digest"] == "accepted"
    assert value["phases"]["reproduction"]["checks"][0]["outcome"] == "failed"
    assert value["phases"]["verification"] == {} and value["phases"]["confirmation"] == {}
    assert (task, records) == before


@pytest.mark.parametrize("argv,method,args", [(["applications"], "applications", ()),
    (["preview-improvement", "--bot", "fixture", "--suite", "suite"], "preview_improvement", ("fixture", "suite")),
    (["comparison", "repair-goal"], "comparison", ("repair-goal",))])
def test_cli_and_console_share_read_controls(argv, method, args, monkeypatch, capsys):
    from chatcopilot.harness import __main__ as cli
    controller = SimpleNamespace(**{method: Mock(return_value={})})
    monkeypatch.setattr(cli, "HarnessController", lambda *a, **kw: controller)
    assert cli.main(argv) == 0
    getattr(controller, method).assert_called_once_with(*args)
    assert capsys.readouterr().out.strip() == "{}"
