"""FC wire and readiness contracts; real local stack is checked separately."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.evals.adapters import agentbench
from chatcopilot.evals.models import EvalCase


def case(index=0):
    return EvalCase(f"dbbench-std:{index}", "Read a value", "db", "Complete",
                    metadata={"adapter": "agentbench-fc", "task": "dbbench-std", "index": index})


def test_start_uses_official_messages_tools_contract_and_terminal_session_is_released(monkeypatch):
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", "http://127.0.0.1:15020/api")
    client = agentbench.Controller()
    requests = []

    def request(operation, payload=None):
        requests.append(operation)
        if operation == "start_sample":
            return {"messages": [{"role": "user", "content": "query"}], "tools": []}, "27"
        return {"finish": True, "status": "completed", "reward": 1, "messages": []}, None

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(client, "sessions", lambda: {"27": {"name": "dbbench-std", "index": 0}})
    assert client.start(case())["finish"] is False
    assert client.session_id == 27
    assert client.call("commit_final_answer", {"answers": ["1"]}, "answer")["finish"]
    assert client.session_id is None
    client.close()
    assert requests == ["start_sample", "interact"]
    with pytest.raises(ValueError, match="completion state"):
        agentbench.validate_observation({"messages": [], "tools": []})


def test_start_does_not_offer_a_failed_environment_to_the_model(monkeypatch):
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", "http://127.0.0.1:15020/api")
    client = agentbench.Controller()
    monkeypatch.setattr(client, "request", lambda *a, **k: ({"messages": [], "tools": []}, "27"))
    monkeypatch.setattr(client, "sessions", lambda: {})
    with pytest.raises(ValueError, match="initialization"):
        client.start(case())
    client.close()


@pytest.mark.parametrize("message,allowed", [("session not found", True), ("invalid session id", False)])
def test_cancel_only_tolerates_the_specific_upstream_not_found_response(monkeypatch, message, allowed):
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", "http://127.0.0.1:15020/api")
    client = agentbench.Controller()

    class Response:
        status_code = 400
        raw = SimpleNamespace(read=lambda *a, **kw: json.dumps({"message": message}).encode())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(client.client, "post", lambda *a, **kw: Response())
    if allowed:
        assert client.request("cancel") == ({}, None)
    else:
        with pytest.raises(ValueError, match="HTTP 400"):
            client.request("cancel")
    client.close()


@pytest.mark.parametrize("worker,indices,ready", [
    ({"status": "ALIVE", "stale": False, "capacity": 1, "current": 0}, [0], True),
    ({"status": "ALIVE", "stale": False, "capacity": 1, "current": 1}, [0], False),
    ({"status": "DEAD", "stale": False, "capacity": 1, "current": 0}, [0], False),
    ({"status": "ALIVE", "stale": True, "capacity": 1, "current": 0}, [0], False),
    ({"status": "ALIVE", "stale": False, "capacity": 1, "current": 0}, ["0"], False),
])
def test_readiness_requires_real_worker_capacity_and_matching_index(monkeypatch, worker, indices, ready):
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", "http://127.0.0.1:15020/api")
    monkeypatch.setattr(agentbench.Controller, "workers", lambda _: {"dbbench-std": {"indices": indices, "workers": {"1": worker}}})
    monkeypatch.setattr(agentbench.Controller, "request", lambda *a, **kw: pytest.fail("readiness must not start/cancel a session"))
    assert agentbench.case_readiness((case(),))[case().case_id]["ready"] is ready


def test_local_deployment_is_loopback_and_keeps_environment_services_separate():
    path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_agentbench.py"
    spec = importlib.util.spec_from_file_location("prepare_agentbench", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    doc = module.compose_document(15020)
    services = doc["services"]
    assert services["controller"]["ports"] == ["127.0.0.1:15020:5020"]
    assert not any("ports" in services[key] for key in ("dbbench", "os", "redis"))
    assert not any("network_mode" in service or "privileged" in service for service in services.values())
    assert services["dbbench"]["mem_limit"] == "512m"
    config = {"default": {"parameters": {"concurrency": 32, "env_options": {"state_options": {}}}}}
    assert module.local_config(config, os_task=False)["default"]["parameters"]["concurrency"] == 1
