"""Native thread identity, structured output and uncertainty are host controlled."""
import json
from pathlib import Path

import pytest

from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.harness import repair_session
from chatcopilot.harness.agent_types import Role, SCHEMAS
from chatcopilot.harness.models import HarnessError, RepairOptions


def test_author_resumes_but_reviewer_starts_independent_threads(tmp_path, monkeypatch):
    seen = []
    def execute(command, **kwargs):
        seen.append(kwargs)
        ident = kwargs["thread_id"] or f"thread-{len(seen)}"
        kwargs["on_thread"](ident)
        kwargs["on_notification"]("turn/started", {"threadId": ident})
        kwargs["on_notification"]("turn/completed", {"threadId": ident, "turn": {"status": "completed"}})
    monkeypatch.setattr(repair_session, "run_app_server", execute)
    author, reviewer = private_directory(tmp_path / "author"), private_directory(tmp_path / "reviewer")
    def run(home, reviewing=False, generation=1, environment_identity="env-1"):
        return repair_session.run_session(["fixture"], root=tmp_path, home=home, environment={}, prompt="frozen evidence",
            options=RepairOptions("test"), task_id="task", generation=generation, role=Role.REVIEW if reviewing else Role.CODING,
            observe=lambda _: None, cancel=lambda: None, environment_identity=environment_identity)
    run(author)
    run(author)
    run(reviewer, True)
    run(reviewer, True)
    assert [r["thread_id"] for r in seen] == ["", "thread-1", "", ""]
    assert seen[0]["output_schema"] == SCHEMAS[Role.CODING]
    assert seen[2]["output_schema"] == SCHEMAS[Role.REVIEW]
    with pytest.raises(HarnessError, match="身份变化"):
        run(author, generation=2)
    with pytest.raises(HarnessError, match="环境"):
        run(author, environment_identity="env-2")
    assert len(seen) == 4


def test_uncertain_acceptance_never_replays_a_turn(tmp_path, monkeypatch):
    calls = []
    def disconnect(command, **kwargs):
        calls.append(1)
        kwargs["on_thread"]("pending")
        raise RuntimeError("connection lost")
    monkeypatch.setattr(repair_session, "run_app_server", disconnect)
    args = dict(root=tmp_path, home=private_directory(tmp_path / "home"), environment={}, prompt="task",
                options=RepairOptions("test"), task_id="task", generation=1, role=Role.CODING,
                observe=lambda _: None, cancel=lambda: None)
    with pytest.raises(RuntimeError):
        repair_session.run_session(["fixture"], **args)
    with pytest.raises(HarnessError, match="未确认"):
        repair_session.run_session(["fixture"], **args)
    assert calls == [1]
    state = json.loads((Path(args["home"]) / "repair-session.json").read_text())
    assert state["state"] == "uncertain"


def test_resumed_thread_usage_is_turn_delta_not_last_request(tmp_path, monkeypatch):
    totals = iter([100, 250])
    observed = []
    def execute(command, **kwargs):
        kwargs["on_thread"]("thread")
        total = next(totals)
        kwargs["on_notification"]("thread/tokenUsage/updated", {"threadId": "thread",
            "tokenUsage": {"total": {"inputTokens": total, "totalTokens": total + 10}, "last": {"inputTokens": 2}}})
        kwargs["on_notification"]("turn/completed", {"threadId": "thread", "turn": {"status": "completed"}})
    monkeypatch.setattr(repair_session, "run_app_server", execute)
    home = private_directory(tmp_path / "home")
    for _ in range(2):
        repair_session.run_session(["fixture"], root=tmp_path, home=home, environment={}, prompt="task",
            options=RepairOptions("test"), task_id="task", generation=1, role=Role.CODING,
            observe=lambda value: observed.append(json.loads(value)), cancel=lambda: None)
    assert [item["usage"]["input_tokens"] for item in observed] == [100, 150]


def test_new_attempt_starts_a_fresh_thread_in_the_same_private_home(tmp_path, monkeypatch):
    seen = []
    def execute(command, **kwargs):
        seen.append(kwargs["thread_id"])
        ident = kwargs["thread_id"] or f"thread-{len(seen)}"
        kwargs["on_thread"](ident)
        kwargs["on_notification"]("turn/completed", {"threadId": ident, "turn": {"status": "completed"}})
    monkeypatch.setattr(repair_session, "run_app_server", execute)
    home = private_directory(tmp_path / "home")
    for attempt in (1, 1, 2):
        repair_session.run_session(["fixture"], root=tmp_path, home=home, environment={}, prompt="task",
            options=RepairOptions("test"), task_id="task", generation=1, role=Role.PLAN, attempt=attempt,
            observe=lambda _: None, cancel=lambda: None)
    assert seen == ["", "thread-1", ""]


def test_large_command_output_is_bounded_before_harness_observation(tmp_path, monkeypatch):
    observed = []
    def execute(command, **kwargs):
        kwargs["on_thread"]("thread")
        kwargs["on_notification"]("item/completed", {"threadId": "thread", "item": {
            "type": "commandExecution", "command": "cat full.log",
            "aggregatedOutput": "a" * (128 * 1024), "exitCode": 0}})
        kwargs["on_notification"]("turn/completed", {"threadId": "thread", "turn": {"status": "completed"}})
    monkeypatch.setattr(repair_session, "run_app_server", execute)
    repair_session.run_session(["fixture"], root=tmp_path, home=private_directory(tmp_path / "home"),
        environment={}, prompt="task", options=RepairOptions("test"), task_id="task", generation=1,
        role=Role.CODING, observe=lambda value: observed.append(json.loads(value)), cancel=lambda: None)
    output = next(item["item"]["aggregated_output"] for item in observed if item["type"] == "item.completed")
    assert len(output) < 66 * 1024
    assert "chars omitted by Harness" in output and output.startswith("a" * 100) and output.endswith("a" * 100)


def test_isolated_evaluation_disables_undeclared_native_image_generation():
    from chatcopilot.evals.isolated_executor import _isolated_subagents
    from chatcopilot.contracts.subagents import SubagentSpec
    policy = _isolated_subagents(SubagentSpec()).codex
    assert not policy.image_generation and not policy.connected_apps and not policy.network_access


_STDIO_SERVER = '''
import json, os, pathlib, signal, sys
root = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
(root / "server.pid").write_text(str(os.getpid()))
if mode == "unresponsive":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
def send(value):
    print(json.dumps(value), flush=True)
for raw in sys.stdin:
    request = json.loads(raw)
    method = request.get("method")
    with (root / "methods.jsonl").open("a") as log:
        log.write(json.dumps(method) + "\\n")
    if method == "initialized":
        continue
    if method == "initialize":
        send({"id": request["id"], "result": {}})
    elif method in ("thread/start", "thread/resume"):
        send({"id": request["id"], "result": {"thread": {"id": "stdio-thread"}, "instructionSources": []}})
    elif method == "turn/start":
        assert "next_role" in request["params"]["outputSchema"]["properties"]
        send({"id": request["id"], "result": {"turn": {"id": "stdio-turn"}}})
        send({"method": "turn/started", "params": {"threadId": "stdio-thread", "turn": {"id": "stdio-turn"}}})
        if mode == "complete":
            result = {"next_role": "plan", "summary": "Inspect the first supported finding", "unresolved": []}
            send({"method": "item/completed", "params": {"threadId": "stdio-thread", "turnId": "stdio-turn",
                "item": {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(result)}}})
            send({"method": "turn/completed", "params": {"threadId": "stdio-thread", "turn": {"id": "stdio-turn", "status": "completed"}}})
    elif method == "turn/interrupt" and mode != "unresponsive":
        send({"id": request["id"], "result": {}})
        send({"method": "turn/completed", "params": {"threadId": "stdio-thread", "turn": {"id": "stdio-turn", "status": "interrupted"}}})
'''


def _run_stdio_session(tmp_path, mode, *, timeout_seconds, cancel=lambda: None):
    import os
    import sys
    script = tmp_path / "stdio_server.py"
    script.write_text(_STDIO_SERVER)
    events = []
    state = repair_session.run_session([sys.executable, str(script), str(tmp_path), mode],
        root=tmp_path, home=private_directory(tmp_path / "session"), environment=dict(os.environ),
        prompt="Find one supported issue", options=RepairOptions("fixture", timeout_seconds=timeout_seconds),
        task_id="stdio-fixture", generation=1, role=Role.MAIN, governance=True,
        observe=lambda line: events.append(json.loads(line)), cancel=cancel)
    return state, events


def _assert_stdio_exited(tmp_path):
    import os
    pid = int((tmp_path / "server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_no_deadline_runs_real_stdio_transport_and_returns_structured_role(tmp_path):
    from chatcopilot.harness.agent_types import role_result
    state, events = _run_stdio_session(tmp_path, "complete", timeout_seconds=None)
    assert state["state"] == "completed" and state["thread_id"] == "stdio-thread"
    output = next(event["item"]["text"] for event in events if event.get("item", {}).get("type") == "agent_message")
    assert role_result(Role.MAIN, json.loads(output), governance=True)["next_role"] == "plan"
    methods = [json.loads(line) for line in (tmp_path / "methods.jsonl").read_text().splitlines()]
    assert methods == ["initialize", "initialized", "thread/start", "turn/start"]
    _assert_stdio_exited(tmp_path)


@pytest.mark.parametrize("mode", ["wait", "unresponsive"])
def test_no_deadline_cancellation_still_bounds_interrupt_and_process_cleanup(tmp_path, mode):
    import time
    from chatcopilot.harness.models import Cancelled
    def cancel():
        path = tmp_path / "session/repair-session.json"
        if path.exists() and json.loads(path.read_text()).get("accepted"):
            raise Cancelled()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        _run_stdio_session(tmp_path, mode, timeout_seconds=None, cancel=cancel)
    assert time.monotonic() - started < 8
    _assert_stdio_exited(tmp_path)
    assert '"turn/interrupt"' in (tmp_path / "methods.jsonl").read_text()
    state = json.loads((tmp_path / "session/repair-session.json").read_text())
    assert state["state"] == ("uncertain" if mode == "unresponsive" else "interrupted")


def test_finite_session_deadline_still_times_out_and_cleans_process(tmp_path):
    import subprocess
    with pytest.raises(subprocess.TimeoutExpired):
        _run_stdio_session(tmp_path, "wait", timeout_seconds=2)
    _assert_stdio_exited(tmp_path)
    state = json.loads((tmp_path / "session/repair-session.json").read_text())
    assert state["state"] == "interrupted"
