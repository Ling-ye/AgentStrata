"""A live socket must own the exact application and verifier; secrets stay off argv."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from studio import runtime


@pytest.fixture
def service(tmp_path, monkeypatch):
    repo, state, verifier = (tmp_path / name for name in ("source", "state", "verifier"))
    for path in (repo, state, verifier):
        path.mkdir(mode=0o700)
    socket = tmp_path / "service.sock"
    monkeypatch.setattr(runtime, "verification_runtime", lambda _: (verifier, "a" * 64))
    monkeypatch.setattr(runtime, "socket_path", lambda *args: socket)
    monkeypatch.setattr(runtime, "configuration", lambda: {})
    host = SimpleNamespace(settings={})
    monkeypatch.setattr(runtime, "HarnessController", lambda *a, **k: host)
    monkeypatch.setattr("chatcopilot.harness.worker_runtime.SystemdWorkerControl", lambda *a, **k: None)
    health = Mock(return_value={"repository_root": str(repo), "verification_root": str(verifier),
                               "artifact_root": str(state / "eval-service")})
    monkeypatch.setattr("chatcopilot.evals.service.EvaluationServiceClient", lambda _: SimpleNamespace(health=health))
    launch = Mock(return_value=SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(runtime.subprocess, "run", launch)
    return SimpleNamespace(repo=repo, state=state, verifier=verifier, host=host, health=health, launch=launch)


@pytest.mark.parametrize("key", ["repository_root", "verification_root", "artifact_root"])
def test_existing_socket_cannot_be_reused_for_different_owner(service, key):
    s = service
    s.health.return_value[key] = "/different"
    with pytest.raises(ValueError, match="不匹配"):
        runtime.controller(s.repo, s.state, start_eval=True)
    s.launch.assert_not_called()


def test_model_environment_is_private_and_same_for_controller_and_evaluator(service):
    s = service
    s.health.side_effect = [FileNotFoundError(), s.health.return_value]
    environment = {"AGENTSTRATA_LLM_CONFIG": "/selected/models.json", "SYNTHETIC_KEY": "private-synthetic-value"}
    host = runtime.controller(s.repo, s.state, start_eval=True, environment=environment)
    assert host.settings == environment
    command = s.launch.call_args.args[0]
    assert "private-synthetic-value" not in str(command)
    path = s.state / "eval-service.env"
    assert "--property=EnvironmentFile=" + str(path) in command
    values = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert json.loads(values["SYNTHETIC_KEY"]) == environment["SYNTHETIC_KEY"]
    assert path.stat().st_mode & 0o777 == 0o600
