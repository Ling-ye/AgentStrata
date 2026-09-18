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


def test_isolated_evaluation_disables_undeclared_native_image_generation():
    from chatcopilot.evals.isolated_executor import _isolated_subagents
    from chatcopilot.contracts.subagents import SubagentSpec
    policy = _isolated_subagents(SubagentSpec()).codex
    assert not policy.image_generation and not policy.connected_apps and not policy.network_access
