"""Local storage and public query adapter boundaries (no model execution)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatcopilot.core.trace_archive import TraceArchive
from chatcopilot.core.trace_capture import TraceCapture
from chatcopilot.evals.service.client import EvaluationServiceClient
from chatcopilot.evals.service.server import EvaluationServiceRuntime
from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.store import HarnessStore


def test_gateway_trace_http_is_run_bound_and_not_cached(tmp_path, monkeypatch):
    from console.backend.routes import architecture
    from console.control import gateway_observability
    (tmp_path / "gateway").mkdir(mode=0o700)
    store = ObservationStore(tmp_path / "gateway", writable=True)
    references = []
    for name in ("run-a", "run-b"):
        store.project_run({"run_id": name, "state": "failed", "created_at": 1, "updated_at": 2})
        capture = TraceCapture({"kind": "robot_task", "run_id": name})
        capture.record({"kind": "execution_input"}, {"text": name})
        ref = TraceArchive(store.root / "traces").save(capture, "failed")
        store.set_meta("trace:" + name, ref)
        references.append(ref)
    monkeypatch.setattr(architecture, "get_instance", lambda _: SimpleNamespace(instance_id="fixture"))
    monkeypatch.setattr(gateway_observability, "reader", lambda _: store)
    app = FastAPI()
    app.include_router(architecture.router)
    with TestClient(app) as client:
        base = "/api/bots/fixture/gateway-observation/runs/"
        response = client.get(base + "run-a/trace")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        a = response.json()["spans"][0]["id"]
        assert client.get(base + f"run-a/trace/steps/{a}").json()["output"]["text"] == "run-a"
        b = client.get(base + "run-b/trace").json()["spans"][0]["id"]
        assert client.get(base + f"run-a/trace/steps/{b}").status_code == 400


def test_harness_reader_uses_registered_source_and_digest(tmp_path):
    store = HarnessStore(tmp_path / "harness")
    for task_id in ("task-a", "task-b"):
        store.create({"task_id": task_id, "request_key": task_id, "match_key": task_id,
                      "context_key": task_id, "active_key": task_id})
    capture = TraceCapture({"kind": "harness", "task_id": "task-a", "phase": "coding"})
    capture.record({"kind": "coding_request"}, {"prompt": "synthetic"})
    root = store.root / "jobs" / "task-a" / "traces"
    archive = TraceArchive(root)
    controller = HarnessController.__new__(HarnessController)
    controller.store = store
    store.register_trace("task-a", root, {"trace_ref": capture.ref, "capture_state": "recording",
        "source": capture.source, "started_at": capture.started, "finished_at": None, "expires_at": None})
    assert controller.trace_record("task-a", capture.ref)["capture_state"] == "recording"
    assert controller.trace_records("task-a")[0]["finished_at"] is None
    reference = archive.save(capture, "completed", retained=True)
    store.register_trace("task-a", root, reference)
    assert controller.trace_record("task-a", reference["trace_ref"])["capture_state"] == "available"
    with pytest.raises(HarnessError, match="没有"):
        controller.trace_record("task-b", reference["trace_ref"])
    path = archive.directory(reference["trace_ref"]) / "trace.json"
    value = json.loads(path.read_text())
    value["name"] = "tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest"):
        controller.trace_record("task-a", reference["trace_ref"])


def test_evaluation_large_step_uses_bounded_service_frames(tmp_path, monkeypatch):
    payload = {"output": "synthetic" * (5 * 1024 * 1024)}
    requested = []
    def trace_record(case_id, *, span_id):
        requested.append((case_id, span_id))
        return payload
    runtime = EvaluationServiceRuntime(repository_root=tmp_path, artifact_root=tmp_path / "evals",
        application=SimpleNamespace(trace_record=trace_record))
    client = EvaluationServiceClient(socket_path=tmp_path / "unused.sock")
    def stream(operation, request):
        for item in runtime.stream(operation, request):
            assert len(json.dumps(item).encode()) < 1024 * 1024
            yield item
    monkeypatch.setattr(client, "_stream", stream)
    result = client.trace_record("case-fixture", span_id="span-fixture")
    assert result == payload
    assert requested == [("case-fixture", "span-fixture")]
