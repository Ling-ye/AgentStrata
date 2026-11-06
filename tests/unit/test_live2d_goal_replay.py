"""Real Gateway and avatar MCP with a controlled model; native rendering is separate."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from chatcopilot.core.llm_client import ChatResult, LLMClient
from chatcopilot.evals.agent_case import CaseCapabilityUnavailable, case_identity, evaluation_cases, validate_case
from chatcopilot.evals.frozen_agent_scoring import score
from chatcopilot.evals.live2d_replay import run
from chatcopilot.evals.trial_capture import capture
from test_application_bridge_evaluation import bridge_bot as bridge_bot


def declaration(**updates):
    return {"schema": "agentstrata.agent-case/v1", "title": "Avatar cue", "input": "work cue",
            "expected_behavior": "Execute the happy expression before replying Ready",
            "bridge": "live2d", "runtime_replay": True,
            "allowed_tools": ["avatar_set_expression"],
            "assertions": [{"kind": "avatar_executed", "action": "expression", "name": "happy"},
                           {"kind": "final_contains", "value": "Ready"}], **updates}


@pytest.fixture
def observed(bridge_bot, tmp_path, monkeypatch):
    messages = []
    def chat(_self, **kwargs):
        messages.append(kwargs["messages"])
        if len(messages) == 1:
            return ChatResult(tool_calls=[{"id": "happy-call", "type": "function", "function": {
                "name": "tool_call", "arguments": json.dumps({"name": "avatar_set_expression", "arguments": {"name": "happy"}})}}])
        return ChatResult(content="Ready")
    monkeypatch.setattr(LLMClient, "chat", chat)
    data = declaration()
    case = evaluation_cases({"snapshot_id": case_identity(data), "case": data})[0]
    with capture():
        observation = run(case, bot=str(bridge_bot), workspace_root=tmp_path / "replay")
    assert "Execute the happy expression before replying Ready" not in json.dumps(messages[0])
    return case, observation


def test_avatar_case_traverses_real_runtime_and_waits_for_execution(observed):
    case, observation = observed
    verdict, evidence = score(case, observation)
    assert verdict.passed, evidence
    assert observation.post_state["bridge"]["avatar_actions"][0]["value"] == 0
    replay = observation.evidence[-1]["runtime_replay"]
    assert set(replay["layers"]) == {"gateway", "application", "agent"}
    assert not replay["production_delivery"]


@pytest.mark.parametrize("fault", ["no_effect", "wrong_id", "unmatched", "reply_first", "before_input", "wrong_value"])
def test_reply_and_tool_success_cannot_replace_ordered_avatar_evidence(observed, fault):
    case, observation = observed
    state = deepcopy(observation.post_state)
    bridge = state["bridge"]
    if fault == "no_effect":
        bridge["avatar_actions"] = []
    elif fault == "wrong_id":
        next(e for e in bridge["timeline"] if e["event"] == "avatar_ack")["request_id"] = "not-the-action"
    elif fault == "unmatched":
        next(e for e in bridge["timeline"] if e["event"] == "avatar_ack")["matched"] = False
    elif fault == "reply_first":
        bridge["timeline"].sort(key=lambda e: e["event"] != "client_reply")
    elif fault == "before_input":
        bridge["timeline"].sort(key=lambda e: e["event"] == "client_input")
    else:
        bridge["avatar_actions"][0]["value"] = 99
    assert not score(case, replace(observation, post_state=state))[0].passed


@pytest.mark.parametrize("changes", [
    {"bridge": "untrusted"}, {"role": "user"}, {"context": "Ready"},
    {"runtime_replay": False}, {"allowed_tools": ["write_workspace_file"]},
    {"fixtures": {"answer.txt": "Ready"}},
])
def test_live2d_fixture_does_not_admit_other_authorities_or_resources(changes):
    with pytest.raises((ValueError, CaseCapabilityUnavailable)):
        validate_case(declaration(**changes))


def test_avatar_assertion_requires_the_declared_writer():
    with pytest.raises(ValueError, match="allowed tools"):
        validate_case(declaration(allowed_tools=["avatar_list_actions"]))


def test_goal_case_keeps_the_client_delivery_contract(observed):
    case, observation = observed
    state = deepcopy(observation.post_state)
    state["bridge"]["runs"][0]["client_reply"]["text"] = "different reply"
    assert not score(case, replace(observation, post_state=state))[0].passed


def test_unrelated_effect_cannot_replace_the_invoked_action_receipt(observed):
    case, observation = observed
    calls = deepcopy(observation.tool_calls)
    call = next(c for c in calls if c["name"] == "avatar_set_expression")
    call["result"]["data"]["content"]["structured"]["request_id"] = "unrelated-invocation"
    assert not score(case, replace(observation, tool_calls=calls))[0].passed


def test_empty_tool_declaration_never_opens_the_avatar_catalog(bridge_bot, tmp_path, monkeypatch):
    calls = []
    def chat(_self, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ChatResult(tool_calls=[{"id": "unavailable", "type": "function", "function": {
                "name": "tool_call", "arguments": json.dumps({"name": "avatar_set_expression", "arguments": {"name": "happy"}})}}])
        return ChatResult(content="Ready")
    monkeypatch.setattr(LLMClient, "chat", chat)
    data = declaration(allowed_tools=[], assertions=[{"kind": "final_contains", "value": "Ready"}])
    case = evaluation_cases({"snapshot_id": case_identity(data), "case": data})[0]
    with capture():
        observation = run(case, bot=str(bridge_bot), workspace_root=tmp_path / "empty")
    assert observation.post_state["bridge"]["avatar_actions"] == []
    assert not any(call["ok"] and call["name"] == "avatar_set_expression" for call in observation.tool_calls)
