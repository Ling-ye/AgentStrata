"""Space diagnostics and explicit retry contracts; no remote inference is performed."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sys
from types import SimpleNamespace

import httpx
import pytest

from integrations.live2d_generator.agentstrata_live2d.config import Config
from integrations.live2d_generator.agentstrata_live2d.providers import SeeThroughSpace, space_failure
from integrations.live2d_generator.agentstrata_live2d.runtime import JobRuntime
from integrations.live2d_generator.agentstrata_live2d.types import GenerationError, GenerationRequest
from tests.unit.test_live2d_generation import png, service_fixture


QUOTA = "You have exceeded your ZeroGPU quota (180s requested vs. 125s left). Try again in 3:23:56."


@pytest.mark.parametrize("authenticated", [False, True])
def test_quota_retains_actionable_numbers_and_drops_remote_secrets(authenticated):
    raw = QUOTA + " credential-canary <script>untrusted-markup</script>"
    error = space_failure(ValueError(raw), authenticated=authenticated, key_env="FIXTURE_DECOMPOSE_KEY")
    assert error.code == "space_quota_exhausted"
    assert error.retry_after_seconds == 12236
    assert "180" in str(error) and "125" in str(error)
    assert ("匿名调用" in str(error)) is not authenticated
    assert "credential-canary" not in str(error) and "untrusted-markup" not in str(error)


@pytest.mark.parametrize(("status", "code"), [
    (401, "space_auth_failed"), (403, "space_auth_failed"), (404, "space_unavailable"),
    (429, "space_rate_limited"), (502, "space_unavailable"), (503, "space_unavailable"),
    (504, "space_unavailable"), (500, "space_execution_failed"),
])
def test_http_failure_classification_does_not_expose_response_details(status, code):
    request = httpx.Request("GET", "https://example.com/private")
    response = httpx.Response(status, request=request)
    exc = httpx.HTTPStatusError("credential-canary", request=request, response=response)
    error = space_failure(exc, authenticated=True, key_env="FIXTURE_KEY")
    assert error.code == code and "credential-canary" not in str(error)


@pytest.mark.parametrize(("exception", "code"), [
    (httpx.ReadTimeout("credential-canary"), "space_timeout"),
    (TimeoutError("credential-canary"), "space_timeout"),
    (httpx.ConnectError("credential-canary"), "space_unavailable"),
    (ValueError("Could not find api_name credential-canary"), "space_protocol_error"),
    (RuntimeError("credential-canary"), "space_execution_failed"),
])
def test_transport_and_protocol_failures_are_distinguished(exception, code):
    error = space_failure(exception, authenticated=False, key_env="FIXTURE_KEY")
    assert error.code == code and "credential-canary" not in str(error)


def test_invalid_retry_time_is_not_persisted():
    error = space_failure(ValueError("GPU quota exceeded. Try again in 3:99:99."),
                          authenticated=False, key_env="FIXTURE_KEY")
    assert error.code == "space_quota_exhausted" and error.retry_after_seconds is None


def test_space_submits_once_and_preserves_quota_failure(tmp_path, monkeypatch):
    calls = []
    class Job:
        def done(self): return True
        def result(self): raise ValueError(QUOTA)
        def cancel(self): calls.append("cancel")
    class Client:
        src = "https://space.example.com/"
        headers = {}
        def __init__(self, *args, **kwargs):
            assert kwargs["token"] is False
            assert kwargs["httpx_kwargs"]["timeout"] == 77
        def submit(self, *args, **kwargs):
            calls.append("submit")
            return Job()
        def close(self): calls.append("close")
    monkeypatch.delenv("AGENTSTRATA_LIVE2D_DECOMPOSE_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "gradio_client", SimpleNamespace(Client=Client, handle_file=str))
    with pytest.raises(GenerationError) as failure:
        SeeThroughSpace(Config(decompose_space="fixture/space", timeout_seconds=77)).decompose(
            tmp_path / "input.png", tmp_path / "output.psd", lambda: None)
    assert failure.value.code == "space_quota_exhausted"
    assert calls.count("submit") == 1 and calls[-1] == "close"
    assert not (tmp_path / "output.psd").exists()


def test_invalid_space_output_reports_protocol_failure(tmp_path, monkeypatch):
    class Client:
        src = "https://space.example.com/"
        headers = {}
        def __init__(self, *args, **kwargs): pass
        def submit(self, *args, **kwargs):
            return SimpleNamespace(done=lambda: True, result=lambda: [{"path": "untrusted-private-path"}, []])
        def close(self): pass
    monkeypatch.setitem(sys.modules, "gradio_client", SimpleNamespace(Client=Client, handle_file=str))
    with pytest.raises(GenerationError) as failure:
        SeeThroughSpace(Config(decompose_space="fixture/space")).decompose(
            tmp_path / "input.png", tmp_path / "output.psd", lambda: None)
    assert failure.value.code == "space_protocol_error"
    assert "untrusted-private-path" not in str(failure.value)


def test_generation_records_safe_error_code_and_absolute_retry_time(tmp_path, monkeypatch):
    service, _ = service_fixture(tmp_path)
    error = space_failure(ValueError(QUOTA + " credential-canary"), authenticated=False, key_env="FIXTURE_KEY")
    def fail(*args): raise error
    service.layers.decompose = fail
    monkeypatch.setattr("integrations.live2d_generator.agentstrata_live2d.service.time.time", lambda: 10000)
    identifier = service.submit(GenerationRequest("fixture", "brief"), None)
    state = service.run(identifier)
    assert state["status"] == "failed" and state["stage"] == "decomposition"
    assert state["error_code"] == "space_quota_exhausted"
    assert state["retry_at"] == datetime.fromtimestamp(22236, timezone.utc).isoformat()
    assert "credential-canary" not in json.dumps(state)
    assert (service.repository.path(identifier) / "illustration.png").is_file()


def test_retry_uses_saved_illustration_without_another_image_request(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d import runtime as module
    runtime = JobRuntime(tmp_path / "jobs", Config(decompose_space="fixture/space"))
    role = runtime.characters.create({"name": "角色"})
    original = tmp_path / "original.png"
    png(original, (16, 24))
    identifier = runtime.repository.create({"name": "角色", "prompt": "edit", "information": {}, "redraw": True}, original)
    root = runtime.repository.path(identifier)
    png(root / "illustration.png")
    before = runtime.repository.read(identifier) | {"status": "failed", "stage": "decomposition", "character_id": role["id"]}
    runtime.repository.save(identifier, before)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: 0))
    monkeypatch.setattr(runtime, "_watch", lambda *args: None)
    with runtime:
        retried = runtime.retry(identifier)
        copied = runtime.repository.path(retried["id"])
        request = json.loads((copied / "request.json").read_text())
        assert retried["retry_of"] == identifier and retried["character_id"] == role["id"]
        assert request["redraw"] is False
        assert (copied / "input.png").read_bytes() == (root / "illustration.png").read_bytes()
        assert runtime.repository.read(identifier) == before
        assert len(runtime.characters.list()) == 1


def test_retry_copies_original_and_refuses_completed_job(tmp_path, monkeypatch):
    from integrations.live2d_generator.agentstrata_live2d import runtime as module
    runtime = JobRuntime(tmp_path / "jobs", Config(decompose_space="fixture/space"))
    original = tmp_path / "original.png"
    png(original)
    identifier = runtime.repository.create({"name": "角色", "prompt": "", "information": {}, "redraw": False}, original)
    state = runtime.repository.read(identifier) | {"status": "failed", "stage": "decomposition"}
    runtime.repository.save(identifier, state)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: 0))
    monkeypatch.setattr(runtime, "_watch", lambda *args: None)
    with runtime:
        retried = runtime.retry(identifier)
        assert (runtime.repository.path(retried["id"]) / "input.png").read_bytes() == original.read_bytes()
        runtime.repository.save(identifier, state | {"status": "completed"})
        with pytest.raises(GenerationError, match="仅可重试"):
            runtime.retry(identifier)


def test_retry_http_requires_same_origin_auth_before_task_access(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from integrations.live2d_generator.agentstrata_live2d.web import create_app
    runtime = JobRuntime(tmp_path / "jobs", Config())
    calls = []
    monkeypatch.setattr(runtime, "retry", lambda identifier: calls.append(identifier) or {"id": "f" * 32})
    with TestClient(create_app(runtime), base_url="http://127.0.0.1:12394") as client:
        assert client.post("/api/jobs/unread/retry").status_code == 403
        assert not calls
        token = re.search(r'name="generator-token" content="([^"]+)"', client.get("/").text)[1]
        headers = {"x-generator-token": token}
        assert client.post("/api/jobs/" + "a" * 32 + "/retry", headers=headers).status_code == 200
        assert calls == ["a" * 32]
        assert client.post("/api/jobs/unread/retry", headers=headers | {"Origin": "https://example.com"}).status_code == 403
        assert len(calls) == 1


def test_capabilities_report_auth_presence_without_exposing_credential(monkeypatch):
    monkeypatch.setenv("AGENTSTRATA_LIVE2D_DECOMPOSE_KEY", "credential-canary")
    capabilities = Config(decompose_space="fixture/space").capabilities()
    assert capabilities["decompose_provider"] == "space" and capabilities["decompose_key_configured"]
    assert "credential-canary" not in json.dumps(capabilities)


@pytest.mark.parametrize("during_connection", [False, True])
def test_space_timeout_identifies_whether_upload_could_have_started(tmp_path, monkeypatch, during_connection):
    calls = []
    class Job:
        def done(self): return True
        def result(self): raise httpx.ReadTimeout("credential-canary")
        def cancel(self): calls.append("cancel")
    class Client:
        def __init__(self, *args, **kwargs):
            if during_connection:
                raise httpx.ReadTimeout("credential-canary")
        def submit(self, *args, **kwargs):
            calls.append("submit")
            return Job()
        def close(self): calls.append("close")
    monkeypatch.setitem(sys.modules, "gradio_client", SimpleNamespace(Client=Client, handle_file=str))
    with pytest.raises(GenerationError) as failure:
        SeeThroughSpace(Config(decompose_space="fixture/space")).decompose(
            tmp_path / "input.png", tmp_path / "output.psd", lambda: None)
    assert failure.value.code == "space_timeout"
    assert ("尚未上传" in str(failure.value)) is during_connection
    assert ("可能仍在执行" in str(failure.value)) is not during_connection
    assert "credential-canary" not in str(failure.value)
    assert calls.count("submit") == (0 if during_connection else 1)
