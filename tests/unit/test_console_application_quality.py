"""Both scoped repair entry points freeze their own source and reject other applications."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from chatcopilot.core.trace_archive import TraceArchive
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.trace_capture import TraceCapture
from chatcopilot.evals.case_instances import case_instance_identity
from chatcopilot.evals.models import RESULT_SCHEMA_VERSION
from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.evaluation_adapter import ServiceEvaluator
from chatcopilot.harness.gateway_adapter import task_source
from console.backend.routes import application_quality
from tests.harness_delivery_fixture import offline_harness_delivery  # noqa: F401


@pytest.fixture
def quality(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(application_quality.router)
    app.state.applications = Mock()
    app.state.applications.require.side_effect = lambda ident: {
        "id": ident,
        "source": "repository",
        "instance_id": ident.removeprefix("repo:"),
    }
    client = app.state.evaluations = Mock()
    trial = {
        "trial_id": "trial-one",
        "case_id": "question",
        "case_ref": "suite:question",
        "target_id": "main",
        "attempt": 1,
        "outcome": "failed",
        "assessment": {},
        "execution": {"metadata": {}},
    }
    ident = case_instance_identity("eval-one", trial)
    trace = TraceArchive(tmp_path / "traces")
    capture = TraceCapture({"kind": "evaluation_case", **ident})
    capture.record({"kind": "execution_input"}, {"text": "fixture question"})
    reference = trace.save(capture, "completed")
    trial["execution"]["metadata"]["trace"] = reference
    trial["case_instance_id"] = ident["case_instance_id"]
    client.case_instance.return_value = {**ident, "trial": trial}
    client.case_detail.return_value = {"trials": [trial]}
    client.get.return_value = {
        "evaluation_id": "eval-one",
        "bot_id": "alpha",
        "status": "completed",
        "conditions": {
            "cases": {"question": "fixture"},
            "targets": {"main": {"model": "fixture"}},
            "grading": {},
        },
        "request": {"kind": "suite", "suite_id": "suite", "bot_id": "alpha", "repetitions": 1},
        "result": {
            "schema_version": RESULT_SCHEMA_VERSION,
            "trials": [trial],
            "targets": [{"target_id": "main", "executor": "agent_configured"}],
            "config_snapshot": {
                "definition_snapshot": {
                    "cases": [{"case_id": "question", "input": "fixture question"}]
                }
            },
        },
    }
    client.trace_export.return_value = trace.export(
        reference["trace_ref"], source=capture.source, sha256=reference["sha256"]
    )
    stores = {
        bot: ObservationStore(private_directory(tmp_path / bot), writable=True)
        for bot in ("alpha", "beta")
    }
    now = time.time()
    run = "run-" + "1" * 32
    stores["alpha"].project_run(
        {
            "run_id": run,
            "state": "failed",
            "error_code": "delivery_failed",
            "created_at": now,
            "updated_at": now,
            "finished_at": now,
            "generation": 1,
            "channel": "qq",
            "conversation_kind": "group",
            "receipts": [],
            "outbox": [],
        }
    )
    stores["alpha"].attach_body(run, "input", {"text": "fixture question"})
    repository = tmp_path / "source"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--shared",
            str(Path(__file__).resolve().parents[2]),
            str(repository),
        ],
        check=True,
    )
    host = app.state.harness = HarnessController(
        repository,
        root=tmp_path / "harness",
        evaluator=ServiceEvaluator(client),
        task_reader=lambda bot, key: task_source(stores[bot], bot, key),
    )
    launch = Mock()
    monkeypatch.setattr(host, "_launch", launch)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as http:
        yield SimpleNamespace(
            http=http,
            run=run,
            case=ident["case_instance_id"],
            host=host,
            launch=launch,
            evals=client,
        )


@pytest.mark.parametrize("kind", ["evaluation", "robot_task"])
def test_repair_handoff_is_bound_before_dispatch_and_detail(quality, kind):
    alpha = "/api/applications/repo:alpha/harness"
    beta = "/api/applications/repo:beta/harness"
    source = quality.case if kind == "evaluation" else quality.run
    body = {"kind": kind, "source_id": source, "bot_id": "" if kind == "evaluation" else "alpha"}
    assert quality.http.post(beta + "/sources/load", json=body).status_code == 404
    response = quality.http.post(alpha + "/sources/load", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["bot_id"] == "alpha"
    selected = (
        {"case_instance_id": source}
        if kind == "evaluation"
        else {
            "bot_id": "alpha",
            "run_id": source,
            "feedback": {"expected_behavior": "成功交付这次答复"},
        }
    )
    payload = {"source_kind": kind, "request_id": "req-" + kind, "profile": "worker", **selected}
    assert quality.http.post(beta + "/tasks", json=payload).status_code == 404
    result = quality.http.post(alpha + "/tasks", json=payload)
    assert result.status_code == 200, result.text
    task = result.json()["task_id"]
    assert quality.http.post(alpha + "/tasks", json=payload).json()["task_id"] == task
    assert quality.launch.call_count == 1
    assert (
        quality.host.get(task)["source"]["case_instance_id" if kind == "evaluation" else "run_id"]
        == source
    )
    assert (
        quality.http.get(alpha + "/tasks/" + task + "?summary=true").json()["source"]["bot_id"]
        == "alpha"
    )
    assert quality.http.get(beta + "/tasks").json()["total"] == 0
    for suffix in ("", "/flow", "/commands", "/evidence"):
        assert quality.http.get(beta + "/tasks/" + task + suffix).status_code == 404
    assert quality.http.post(beta + "/tasks/" + task + "/cancel").status_code == 404
    if kind == "robot_task":
        assert (
            quality.http.post(beta + "/sources/load", json={**body, "bot_id": "beta"}).status_code
            == 404
        )


def test_evaluation_operations_use_application_owner(quality):
    base = "/api/applications/repo:beta/evals"
    for suffix in (
        "/evaluations/eval-one",
        "/evaluations/eval-one/cases/suite:question",
        "/case-instances/" + quality.case + "/trace",
    ):
        assert quality.http.get(base + suffix).status_code == 404
    assert quality.http.post(base + "/evaluations/eval-one/cancel").status_code == 404
    quality.evals.cancel.assert_not_called()
    body = {"kind": "suite", "bot_id": "alpha", "suite_id": "suite", "preset": "quick"}
    assert quality.http.post(base + "/evaluations", json=body).status_code == 404
    quality.evals.start.assert_not_called()
    quality.evals.start.return_value = {"evaluation_id": "eval-new", "bot_id": "beta"}
    assert (
        quality.http.post(base + "/evaluations", json={**body, "bot_id": "beta"}).status_code == 200
    )
    assert quality.evals.start.call_args.kwargs["bot_id"] == "beta"


def test_studio_case_repair_uses_its_verified_service_and_durable_source(
    quality, tmp_path, monkeypatch
):
    from uuid import uuid4
    from chatcopilot.core.source_snapshot import git_output
    from console.control import project_evaluation
    from console.control.applications import Applications
    from studio import projects, runtime

    apps = Applications(
        Path(__file__).resolve().parents[2], tmp_path / "private-studio", discover=lambda: []
    )
    quality.http.app.state.applications = apps
    project = uuid4().hex
    studio = apps.studio()
    studio.create(project)
    work = studio.directory(project)
    bot = "app-" + project
    monkeypatch.setattr(projects, "project_inputs", lambda _: {})
    repo = projects.new_project(studio.framework, work, bot, "本机网页", target="web")
    with studio.db.connect(write=True) as db:
        db.execute("UPDATE projects SET version='initial' WHERE id=?", (project,))
    base = "/api/applications/studio:" + project
    assert quality.http.get(base + "/evaluation-service").json()["ready"] is False
    verifier = "a" * 64
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock = projects.socket_path(repo, verifier)
    monkeypatch.setattr(
        runtime,
        "controller",
        lambda *a, **kw: SimpleNamespace(evaluator=SimpleNamespace(verification_runtime=verifier)),
    )
    client = quality.evals
    monkeypatch.setattr(
        project_evaluation, "EvaluationServiceClient", lambda path: client if path == sock else None
    )
    client.health.return_value = {
        "repository_root": str(repo),
        "verification_root": str(work / "verification" / verifier),
        "artifact_root": str(work / ("eval-" + sock.stem)),
    }
    assert quality.http.post(base + "/evaluation-service").json() == {"ready": True, "bot_id": bot}
    original = client.get.return_value
    original["bot_id"] = original["request"]["bot_id"] = bot
    original["source_revision"] = {"commit": git_output(repo, "rev-parse", "HEAD"), "dirty": False}
    selection = {"kind": "evaluation", "source_id": quality.case}
    result = quality.http.get(base + "/project-repairs/source", params=selection)
    assert result.status_code == 200, result.text
    payload = {
        **selection,
        "id": uuid4().hex,
        "digest": result.json()["digest"],
        "feedback": "复测已失败的原用例",
    }
    assert (
        quality.http.post(base + "/project-repairs", json=payload).json()["message_id"]
        == payload["id"]
    )
    assert quality.http.post(base + "/project-repairs", json=payload).status_code == 200
    assert (
        quality.http.post(
            base + "/project-repairs", json={**payload, "feedback": "不同目标"}
        ).status_code
        == 400
    )
    start = Mock(return_value={"task_id": "repair-fixture"})
    monkeypatch.setattr(runtime, "start_task", start)
    studio.step(payload["id"])
    receipt = start.call_args.kwargs["repair_origin"]
    assert receipt["case_instance_id"] == quality.case and receipt["trial"]["outcome"] == "failed"
    client.health.return_value["repository_root"] = str(tmp_path / "other")
    assert quality.http.get(base + "/evals/evaluations/eval-one").status_code == 409


def test_owner_is_rechecked_on_the_material_frozen_for_dispatch(quality, monkeypatch):
    from dataclasses import replace

    original = quality.host.sources.load

    def moved(reference):
        evidence = original(reference)
        return replace(evidence, material={**evidence.material, "bot_id": "beta"})

    monkeypatch.setattr(quality.host.sources, "load", moved)
    result = quality.http.post(
        "/api/applications/repo:alpha/harness/tasks",
        json={
            "case_instance_id": quality.case,
            "request_id": "changed-source",
            "profile": "worker",
        },
    )
    assert result.status_code == 404
    assert quality.host.list()["total"] == 0
    quality.launch.assert_not_called()


def test_private_task_views_require_own_message_and_only_expose_public_facts(
    quality, tmp_path, monkeypatch
):
    from uuid import uuid4
    from console.control.applications import Applications
    from studio import runtime

    apps = Applications(
        Path(__file__).resolve().parents[2], tmp_path / "projects-home", discover=lambda: []
    )
    quality.http.app.state.applications = apps
    studio = apps.studio()
    owner, other, message = (uuid4().hex for _ in range(3))
    task = "repair-" + uuid4().hex
    for project in (owner, other):
        studio.create(project)
    with studio.db.connect(write=True) as db:
        db.execute(
            "INSERT INTO messages(id,project,text,state,version,task,updated) VALUES (?,?,?,'ready','initial',?,1)",
            (message, owner, "修复请求", task),
        )
    host = Mock()
    host.summary.return_value = {"source": {"bot_id": "app-" + owner}}
    host.get.return_value = {"source": {"goal": "修复请求"}}
    host.flow.return_value = {
        "steps": [
            {
                "id": "step",
                "title": "验收",
                "status": "completed",
                "phase": "verify",
                "input_summary": "private worker text",
            }
        ]
    }
    host.commands.return_value = {
        "sources": [{"id": "review", "kind": "review", "number": 1}],
        "source": {"id": "review"},
        "events": [
            {
                "id": "command",
                "command": "pytest private-argument",
                "aggregated_output": "private output",
                "exit_code": 0,
            }
        ],
        "next_cursor": None,
        "has_more": False,
    }
    attach = Mock(return_value=host)
    monkeypatch.setattr(runtime, "controller", attach)

    def endpoint(project, suffix):
        return "/api/applications/studio:" + project + "/project-tasks/" + task + "/" + suffix

    for suffix in ("flow", "commands"):
        assert quality.http.get(endpoint(other, suffix)).status_code == 404
    attach.assert_not_called()
    flow = quality.http.get(endpoint(owner, "flow"))
    logs = quality.http.get(endpoint(owner, "commands"))
    assert (
        flow.json()["steps"][0]["status"] == "completed" and "private worker text" not in flow.text
    )
    assert logs.json()["events"] == [{"id": "command", "program": "pytest", "exit_code": 0}]
    assert logs.json()["selected"] == "review" and "private" not in logs.text
