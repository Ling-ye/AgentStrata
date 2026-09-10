import hashlib
import json
from types import SimpleNamespace

import pytest

from chatcopilot.evals.adapters import agentbench, swebench
from chatcopilot.evals.adapters import swebench_runtime as sandbox
from chatcopilot.evals.environment_cleanup import cleanup_environment
from chatcopilot.evals.models import EvalCase
from chatcopilot.evals.trial_capture import capture


@pytest.mark.parametrize("url", ["https://example.test/api", "http://localhost/api", "http://127.0.0.1/api?x=y", "http://127.0.0.1/elsewhere", "".join(("http", "://", "user", "@", "127.0.0.1/api"))])
def test_agentbench_rejects_nonlocal_or_ambiguous_controller(monkeypatch, url):
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", url)
    with pytest.raises(ValueError, match="loopback"):
        agentbench.controller_url()


def test_agentbench_catalog_is_offline_and_freezes_revision(tmp_path, monkeypatch):
    path = tmp_path / "tasks.jsonl"
    row = {"task": "dbbench-std", "index": 0, "input": "Read a database value", "source_revision": "a" * 40}
    path.write_text(json.dumps(row))
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_DATA_PATH", str(path))
    case, = agentbench.load_cases()
    assert case.case_id == "dbbench-std:0" and case.metadata["source_revision"] == "a" * 40
    path.write_text(json.dumps({**row, "index": -1}))
    with pytest.raises(ValueError, match="task/index"):
        agentbench.load_cases()


def test_controller_publishes_lease_before_reading_body_and_never_follows_redirects(monkeypatch):
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", "http://127.0.0.1:5020/api")
    sent = []
    frames = []

    class Response:
        status_code = 200
        headers = {"session_id": "123"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @property
        def raw(self):
            assert frames[-1]["environment"]["session_id"] == 123
            return SimpleNamespace(read=lambda *a, **k: b'{"finish":false,"messages":[],"tools":[]}')

    with capture(lambda value: frames.append(json.loads(json.dumps(value)))):
        client = agentbench.Controller()
        monkeypatch.setattr(client.client, "post", lambda url, **kwargs: (sent.append((url, kwargs)) or Response()))
        case = EvalCase("db:0", "Task", "db", "Complete", metadata={"task": "dbbench-std", "index": 0})
        client.start(case)
        client.call("execute_sql", {"sql": "select 1"}, "call_0")
        client.close()
    assert client.client.trust_env is False
    assert all(item[1]["allow_redirects"] is False for item in sent)
    assert sent[1][1]["headers"] == {"session_id": "123"}
    assert sent[1][1]["json"]["messages"][0]["tool_calls"][0]["function"]["name"] == "execute_sql"
    assert sent[-1][0].endswith("/cancel")


def test_agentbench_uses_environment_outcome_and_rejects_unknown_success():
    assert agentbench.judge_response({"finish": True, "status": "completed", "reward": 1}).passed
    assert not agentbench.judge_response({"finish": False}).passed
    with pytest.raises(ValueError, match="unknown terminal"):
        agentbench.judge_response({"finish": True, "reward": 1})
    with pytest.raises(ValueError, match="environment error"):
        agentbench.judge_response({"finish": True, "status": "task error", "reward": 1})


def test_cleanup_refuses_controller_drift(monkeypatch):
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", "http://127.0.0.1:5020/api")
    with pytest.raises(ValueError, match="changed"):
        cleanup_environment({"kind": "agentbench-fc", "session_id": 1, "controller_fingerprint": "0" * 64})


def test_swebench_never_passes_gold_patch_to_case_input(tmp_path, monkeypatch):
    path = tmp_path / "swe.jsonl"
    path.write_text(json.dumps({"instance_id": "sample__repo-1", "problem_statement": "Fix the bug.",
        "patch": "gold solution", "base_commit": "a" * 40, "eval_script": "withheld tests", "repo": "sample/repo"}))
    monkeypatch.setenv("CHATCOPILOT_SWEBENCH_DATA_PATH", str(path))
    case, = swebench.load_cases()
    assert case.input == "Fix the bug."
    assert "gold solution" not in json.dumps(case.metadata)
    assert case.metadata["swe_instance"]["eval_script"] == "withheld tests"


def test_swe_container_never_mounts_host_or_pulls_images(monkeypatch):
    commands = []

    def docker(arguments, **kwargs):
        commands.append(arguments)
        return 0, "sha256:" + "b" * 64

    monkeypatch.setattr(sandbox, "docker", docker)
    sandbox.start_container("agentstrata-swe-" + "a" * 32 + "-solve", "prepared-image")
    args = commands[1]
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--pull") + 1] == "never"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "--volume" not in args and "--mount" not in args and "--privileged" not in args


def test_swe_cleanup_checks_ownership_before_removal(monkeypatch):
    name = "agentstrata-swe-" + "a" * 32 + "-grade"
    calls = []

    def docker(arguments, **kwargs):
        calls.append(arguments)
        return 0, json.dumps([{"Config": {"Labels": {"agentstrata.evaluation": "different"}}}])

    monkeypatch.setattr(sandbox, "docker", docker)
    with pytest.raises(ValueError, match="ownership"):
        sandbox.cleanup_container(name)
    assert len(calls) == 1
    with pytest.raises(ValueError, match="identity"):
        sandbox.cleanup_container("some-other-container")


def test_agentbench_plugin_runs_host_agent_with_environment_tools(monkeypatch, tmp_path):
    from chatcopilot.evals.plugins import agentbench as plugin
    monkeypatch.setenv("CHATCOPILOT_AGENTBENCH_CONTROLLER_URL", "http://127.0.0.1:5020/api")
    closed = []

    class Controller:
        url = "http://127.0.0.1:5020/api/"
        session_id = 17

        def start(self, case):
            return {"finish": False, "messages": [{"role": "user", "content": "Query the value"}],
                "tools": [{"function": {"name": "execute_sql", "description": "Query", "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}}]}

        def call(self, name, arguments, call_id):
            assert arguments == {"sql": "select 1"}
            return {"finish": True, "status": "completed", "reward": 1, "messages": []}

        def close(self):
            closed.append(True)

    def run_agent(**kwargs):
        from chatcopilot.contracts.tool_validation import validate_tool_contract
        import jsonschema

        tool, = kwargs["provider"].packs["runtime.session"]
        assert kwargs["tool_names"] == frozenset({"execute_sql"})
        assert not validate_tool_contract(tool)
        result = tool.handler({"sql": "select 1"}, None)
        assert result.ok
        jsonschema.validate(result.data, tool.output_schema)
        return "Completed", []

    monkeypatch.setattr(plugin.agentbench, "Controller", Controller)
    monkeypatch.setattr(plugin, "run_environment_agent", run_agent)
    result = plugin._execute(EvalCase("db:0", "Query", "db", "Complete"), bot="sample", workspace_root=tmp_path, options={"scoring_mode": "native"})
    assert result.status == "passed" and closed
    assert result.metadata["environment_result"]["reward"] == 1
    assert result.metadata["execution"]["environment"]["controller_fingerprint"] == hashlib.sha256(Controller.url.encode()).hexdigest()


def test_real_upstream_swebench_grader_is_called_on_captured_test_output(monkeypatch, tmp_path):
    pytest.importorskip("swebench")
    # Use the actual pinned grader with its real parser; only container transport is replaced.
    from swebench.harness.constants import START_TEST_OUTPUT, END_TEST_OUTPUT
    row = {"instance_id": "sample__repo-1", "image": "prepared", "repo": "sympy/sympy", "version": "1",
           "FAIL_TO_PASS": ["test_case"], "PASS_TO_PASS": [],
           "log_parser": "parse_log_sympy", "eval_type": "pass_and_fail", "eval_script": "echo tests"}
    case = EvalCase("swe-example", "Fix", "code", "Fix", metadata={"instance_id": row["instance_id"], "swe_instance": row})
    def docker(arguments, **kwargs):
        if "apply" in arguments:
            return 0, "applied"
        return 0, f"{START_TEST_OUTPUT}\ntest_case ok\n{END_TEST_OUTPUT}\n"
    monkeypatch.setattr(sandbox, "docker", docker)
    result, report = sandbox.grade(case, "diff --git a/x b/x\n", container="fixture", output=tmp_path)
    assert result.passed and report["resolved"] is True
    assert (tmp_path / "test-output.txt").is_file()
