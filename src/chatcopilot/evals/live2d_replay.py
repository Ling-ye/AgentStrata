"""Declarative goals using the existing production Live2D bridge replay."""
from dataclasses import replace

from chatcopilot.evals.agent_case import validate_case
from chatcopilot.evals.bridge_cases import AVATAR_EXPRESSIONS, AVATAR_MOTIONS
from chatcopilot.evals.models import EvalCaseAssertion, EvalCaseDefinition, EvalCasePolicy, EvalCaseRequirements, EvalCaseTurn


def definition(case):
    declaration = validate_case(case.metadata["agent_case"])
    if declaration.get("bridge") != "live2d":
        raise ValueError("Live2D replay requires the frozen bridge declaration")
    return EvalCaseDefinition(
        schema="agentstrata-eval-case/v1", case_id=case.case_id, version=1,
        capability="application_bridge", plugin_id="application-bridge", driver_id="agent_configured",
        turns=(EvalCaseTurn(case.input),), requirements=EvalCaseRequirements(),
        policy=EvalCasePolicy(side_effect="isolated_write", network="loopback",
                              allowed_tools=tuple(declaration["allowed_tools"])),
        assertions=(EvalCaseAssertion("trusted_verifier", "application_bridge"),),
        quality={"enabled": False}, scenario_id="avatar-interaction", scenario_params={"application": "live2d"},
    )


def run(case, *, bot, workspace_root):
    from chatcopilot.evals.bridge_runtime import run as replay
    observation = replay(definition(case), bot=bot, workspace_root=workspace_root)
    state = observation.post_state["bridge"]
    stages = state["stages"]
    evidence = {"layers": [layer for layer in ("gateway", "application", "agent")
                          if any(stage["layer"] == layer for stage in stages)],
                "admission": "allowed", "production_delivery": False, "stages": stages}
    return replace(observation, evidence=(*observation.evidence, {"kind": "adapter_metadata", "runtime_replay": evidence}))


def verify_delivery(case, observation):
    from chatcopilot.evals.bridge_scoring import verify
    selected = definition(case)
    return verify(selected, selected.assertions[0], observation).passed


def avatar_executed(check, observation):
    state = observation.post_state["bridge"]
    action_kind, name = check["action"], check["name"]
    expected = (AVATAR_EXPRESSIONS if action_kind == "expression" else AVATAR_MOTIONS)[name]
    write_tool = "avatar_set_expression" if action_kind == "expression" else "avatar_play_motion"
    calls = [call for call in observation.tool_calls if call["ok"] and (
            call["name"] == write_tool and call["arguments"].get("name") == name
            or call["name"] == "avatar_apply_pose" and call["arguments"].get(action_kind) == name
            )]
    receipts = set()
    for call in calls:
        result = call.get("result") or {}
        if not isinstance(result, dict):
            continue
        response = result.get("data", {}).get("content", {}).get("structured", {})
        if call["name"] == "avatar_apply_pose":
            response = response.get(action_kind) or {}
        if response.get("status") == "executed" and isinstance(response.get("request_id"), str):
            receipts.add(response["request_id"])
    timeline = state["timeline"]
    inputs = [i for i, event in enumerate(timeline) if event["event"] == "client_input"]
    replies = [i for i, event in enumerate(timeline) if event["event"] == "client_reply"
               and event.get("run_id") in {run["run_id"] for run in state["runs"]}]
    for action in state["avatar_actions"]:
        if action.get("kind") != action_kind or action.get("value") != expected or action.get("request_id") not in receipts:
            continue
        requested = [i for i, event in enumerate(timeline) if event["event"] == "avatar_action"
                     and event.get("request_id") == action["request_id"]]
        acknowledged = [i for i, event in enumerate(timeline) if event["event"] == "avatar_ack"
                        and event.get("request_id") == action["request_id"] and event.get("matched") is True
                        and event.get("status") == "executed"]
        if any(received < start < ack < reply for received in inputs for start in requested
               for ack in acknowledged for reply in replies):
            return True
    return False
