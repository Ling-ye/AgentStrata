"""Formal Eval Trials with real host/bridge adapters and controlled model I/O."""
from __future__ import annotations

from dataclasses import replace
import json
import subprocess

import pytest

from chatcopilot.core.llm_client import ChatResult, LLMClient
from chatcopilot.evals.bridge_cases import SUITE_ID
from chatcopilot.evals.registry import get_cases
from chatcopilot.evals.trial_runner import run_case
from tests.application_fixture import write_application
from tests.codex_app_server_fixture import app_server_replay


@pytest.fixture
def bridge_bot(tmp_path, monkeypatch):
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"connections": {"bridge": {"kind": "openai_responses",
        "auth": {"mode": "api_key", "key_env": "BRIDGE_KEY"}}},
        "profiles": {"bridge": {"connection": "bridge", "model": "bridge-model"}},
        "bindings": {"bridge.chat": "bridge"}}))
    monkeypatch.setenv("AGENTSTRATA_LLM_CONFIG", str(models))
    monkeypatch.setenv("BRIDGE_KEY", "synthetic-bridge-model")
    monkeypatch.setenv("CHATCOPILOT_SOURCE_URL", "https://example.com/source")
    (tmp_path / "identity.md").write_text("Bridge assistant")
    (tmp_path / "style.md").write_text("Answer clearly.")
    path = tmp_path / "bot.yaml"
    write_application(path, {"id": "bridge-eval", "display_name": "Bridge eval",
        "gateway": {"clients": [{"id": "local", "token_env": "LOCAL_TOKEN", "role": "owner"}]},
        "llm": {"chat": {"binding": "bridge.chat"}, "research": {"binding": "bridge.chat"}},
        "prompts": {"schema_version": 2, "identity": "identity.md", "response_style": "style.md"},
        "agents": {"runtime": "langgraph"}, "tools": {"packs": []}})
    return path


def _models(monkeypatch, *, motion=False, pose=False):
    calls = []
    motion_sent = False

    def chat(_self, **kwargs):
        nonlocal motion_sent
        calls.append("langgraph")
        if (motion or pose) and not motion_sent:
            motion_sent = True
            return ChatResult(tool_calls=[{"id": "motion-call", "type": "function", "function": {
                "name": "tool_call", "arguments": json.dumps({"name": "avatarapply_pose" if pose else "avatarplay_motion",
                    "arguments": {"expression": "happy", "motion": "greet"} if pose else {"name": "greet"}})}}])
        return ChatResult(content="桥接春风，桥接已就绪。")

    def codex(command, **kwargs):
        calls.append("codex")
        kwargs["on_stdout_line"](json.dumps({"type": "thread.started", "thread_id": "fixture-thread"}))
        if motion or pose:
            result = kwargs["on_request"]("item/tool/call", {"namespace": "agentstrata", "threadId": "fixture-thread",
                "tool": "avatarapply_pose" if pose else "avatarplay_motion",
                "arguments": {"expression": "happy", "motion": "greet"} if pose else {"name": "greet"}, "callId": "motion-call"})
            assert result["success"], result
        return subprocess.CompletedProcess(command, 0, json.dumps({"type": "item.completed",
            "item": {"type": "agent_message", "text": "桥接春风，桥接已就绪。"}}), "")

    monkeypatch.setattr(LLMClient, "chat", chat)
    monkeypatch.setattr("chatcopilot.external_tools.codex_cli.command._resolve_executable", lambda *_: "/usr/bin/true")
    monkeypatch.setattr("chatcopilot.agent.runtimes.codex.run_app_server", app_server_replay(codex))
    return calls


@pytest.mark.parametrize("runtime_id", ["langgraph", "codex"])
@pytest.mark.parametrize("case_id", ["live2d-dialogue", "qq-dialogue", "live2d-motion", "live2d-pose", "live2d-session-isolation", "qq-admission", "qq-delivery-unknown"])
def test_bridge_cases_use_core_trials_and_selected_runtime(bridge_bot, tmp_path, monkeypatch, runtime_id, case_id):
    import yaml
    profile = bridge_bot.with_name("agent.yaml")
    data = yaml.safe_load(profile.read_text())
    data["agents"]["runtime"] = runtime_id
    profile.write_text(yaml.safe_dump(data, allow_unicode=True))
    calls = _models(monkeypatch, motion=case_id == "live2d-motion", pose=case_id == "live2d-pose")
    case = next(c for c in get_cases(SUITE_ID) if c.case_id == case_id)
    result = run_case(case, suite_id=SUITE_ID, bot=str(bridge_bot), workspace_root=tmp_path / "trial", options={})
    assert result.status == "passed", (result.error, result.judge, result.metadata)
    assert set(calls) == ({runtime_id} if case_id != "qq-admission" else set())
    assert result.metadata["bridge_evidence"]["production_delivery"] is False
    assert result.metadata["judge_evidence"]["failure_kind"] == ""


def test_candidate_cannot_replace_the_frozen_case(bridge_bot, tmp_path):
    case = get_cases(SUITE_ID)[0]
    metadata = {**case.metadata, "case_definition": {**case.metadata["case_definition"], "scenario_id": "admission"}}
    result = run_case(replace(case, metadata=metadata), suite_id=SUITE_ID, bot=str(bridge_bot),
                      workspace_root=tmp_path / "trial", options={})
    assert result.status == "error"
    assert "frozen definition" in result.error.message


def test_bridge_suite_uses_normal_evaluation_preflight_and_trial_identity(bridge_bot, tmp_path, monkeypatch):
    from chatcopilot.evals.evaluations import run_evaluation, validate_evaluation, execute_evaluation_trial
    calls = _models(monkeypatch)
    request = {"kind": "suite", "suite": SUITE_ID, "bot": str(bridge_bot), "case_ids": ["live2d-dialogue"],
               "evaluation_id": "eval-bridge-core", "repetitions": 1, "max_wall_seconds": 30}
    validation = validate_evaluation(request)
    assert validation["ready"], validation["checks"]
    # Exercise the regular Core executor; process supervision is covered separately.
    result = run_evaluation(request, output=tmp_path / "evaluation", trial_executor=execute_evaluation_trial)
    assert result.status == "completed" and result.trials[0].outcome == "passed", result
    assert result.config_snapshot["product_components"]["sha256"]
    assert "version_snapshot" not in result.config_snapshot
    assert calls == ["langgraph", "langgraph"]


@pytest.mark.parametrize("fault,kind", [("model", "agent_error"), ("client", "bridge_error")])
def test_agent_and_bridge_failures_are_scored_separately(bridge_bot, tmp_path, monkeypatch, fault, kind):
    _models(monkeypatch)
    if fault == "model":
        monkeypatch.setattr(LLMClient, "chat", lambda *args, **kwargs: ChatResult(content="wrong answer"))
    else:
        from chatcopilot.evals.bridge_cases import live2d_module
        cls = live2d_module("bridge").ConversationBridge
        original = cls.chat

        async def corrupt(self, text):
            return replace(await original(self, text), text="corrupted reply")
        monkeypatch.setattr(cls, "chat", corrupt)
    case = next(c for c in get_cases(SUITE_ID) if c.case_id == "live2d-dialogue")
    result = run_case(case, suite_id=SUITE_ID, bot=str(bridge_bot), workspace_root=tmp_path / "trial", options={})
    assert result.status == "failed" and result.metadata["judge_evidence"]["failure_kind"] == kind


def test_environment_failure_is_not_scored_as_an_agent_regression(bridge_bot, tmp_path, monkeypatch):
    def unavailable_host(*args, **kwargs):
        raise OSError("controlled unavailable environment")
    monkeypatch.setattr("chatcopilot.evals.bridge_runtime.build_gateway_runtime_host", unavailable_host)
    result = run_case(get_cases(SUITE_ID)[0], suite_id=SUITE_ID, bot=str(bridge_bot),
                      workspace_root=tmp_path / "trial", options={})
    assert result.status == "error" and result.error.code == "environment_error"
    assert result.judge is None


def test_missing_browser_receipt_preserves_action_evidence_without_a_verdict(bridge_bot, tmp_path, monkeypatch):
    from chatcopilot.evals.models import TrialObservation
    case = next(c for c in get_cases(SUITE_ID) if c.case_id == "live2d-motion")
    run = {"session_id": "session", "run_id": "run", "state": "completed", "display_text": "done",
           "client_reply": {"session_id": "session", "run_id": "run", "state": "completed", "text": "done"}}
    state = {"application": "live2d", "scenario": "avatar-action", "production_delivery": False,
             "runs": [run], "avatar_actions": [{"kind": "motion", "request_id": "action", "value": {"group": "TapBody", "index": 0}}],
             "avatar_acknowledgements": []}
    observation = TrialObservation(final_text="done", stop_reason="end_turn", post_state={"bridge": state},
                                   events=({"type": "LlmCallFinished"},),
                                   tool_calls=({"name": "avatarplay_motion", "arguments": {"name": "greet"}},))
    monkeypatch.setattr("chatcopilot.evals.bridge_runtime.run", lambda *args, **kwargs: observation)
    result = run_case(case, suite_id=SUITE_ID, bot=str(bridge_bot), workspace_root=tmp_path / "trial", options={})
    assert result.status == "error" and result.error.code == "evidence_missing"
    assert result.judge is None and result.metadata["post_state"]["bridge"]["avatar_actions"]


def test_pose_efficiency_requires_one_call_and_two_independent_receipts(bridge_bot, tmp_path, monkeypatch):
    from chatcopilot.evals.models import TrialObservation
    case = next(c for c in get_cases(SUITE_ID) if c.case_id == 'live2d-pose')
    run = {'session_id': 'session', 'run_id': 'run', 'state': 'completed', 'display_text': 'done',
           'client_reply': {'session_id': 'session', 'run_id': 'run', 'state': 'completed', 'text': 'done'}}
    state = {'application': 'live2d', 'scenario': 'avatar-pose', 'production_delivery': False,
             'runs': [run], 'avatar_actions': [
                 {'kind': 'expression', 'request_id': 'expression', 'value': 0},
                 {'kind': 'motion', 'request_id': 'motion', 'value': {'group': 'TapBody', 'index': 0}}],
             'avatar_acknowledgements': [{'request_id': kind, 'matched': True, 'status': 'executed'} for kind in ('expression', 'motion')]}
    observation = TrialObservation(final_text='done', stop_reason='end_turn', post_state={'bridge': state},
        events=({'type': 'LlmCallFinished'},), tool_calls=({'name': 'avatarset_expression'}, {'name': 'avatarplay_motion'}))
    monkeypatch.setattr('chatcopilot.evals.bridge_runtime.run', lambda *args, **kwargs: observation)
    baseline = run_case(case, suite_id=SUITE_ID, bot=str(bridge_bot), workspace_root=tmp_path / 'base', options={})
    assert baseline.status == 'failed' and 'one_writing_tool_call' in baseline.judge.violations
    observation = replace(observation, tool_calls=({'name': 'avatarapply_pose'},))
    candidate = run_case(case, suite_id=SUITE_ID, bot=str(bridge_bot), workspace_root=tmp_path / 'candidate', options={})
    assert candidate.status == 'passed'
    state['avatar_acknowledgements'].pop()
    missing = run_case(case, suite_id=SUITE_ID, bot=str(bridge_bot), workspace_root=tmp_path / 'missing', options={})
    assert missing.status == 'error' and missing.error.code == 'evidence_missing'


def test_runtime_startup_failure_is_environment_evidence_not_agent_score(bridge_bot, tmp_path, monkeypatch):
    import yaml
    profile = bridge_bot.with_name('agent.yaml')
    declaration = yaml.safe_load(profile.read_text())
    declaration['agents']['runtime'] = 'codex'
    profile.write_text(yaml.safe_dump(declaration))
    _models(monkeypatch)
    def disconnected(*args, **kwargs):
        raise RuntimeError('controlled app-server startup failure')
    monkeypatch.setattr('chatcopilot.agent.runtimes.codex.run_app_server', disconnected)
    case = next(c for c in get_cases(SUITE_ID) if c.case_id == 'live2d-dialogue')
    result = run_case(case, suite_id=SUITE_ID, bot=str(bridge_bot), workspace_root=tmp_path / 'failed-runtime', options={})
    assert result.status == 'error' and result.error.code == 'model_execution_error'
    assert result.judge is None
