"""Independent, deterministic bridge facts; adapters never supply pass/fail."""
from __future__ import annotations

from chatcopilot.evals.bridge_cases import unavailable, validate
from chatcopilot.evals.models import AssertionOutcome, JudgeResult


def verify(definition, assertion, observation):
    validate(definition)
    state = observation.post_state.get("bridge")
    if (not isinstance(state, dict) or state.get("production_delivery") is not False
            or state.get("application") != definition.scenario_params["application"]
            or state.get("scenario") != definition.scenario_id):
        raise unavailable("Missing or mismatched bridge observations", code="evidence_missing")
    checks = {}
    failure_kind = "bridge_error"
    scenario = definition.scenario_id
    runs, actions, receipts = (state.get(key, []) for key in ("runs", "provider_actions", "receipts"))
    if scenario == "admission":
        checks = {"admission_denied": state.get("admission_denied") is True,
                  "no_session": state.get("session_count") == 0, "no_delivery": not actions}
    else:
        if len(runs) != len(definition.turns) or any(not r.get("run_id") or not r.get("session_id") for r in runs):
            raise unavailable("Incomplete Gateway run evidence", code="evidence_missing")
        if not any(event.get("type") == "LlmCallFinished" for event in observation.events):
            raise unavailable("Agent execution observations are missing", code="evidence_missing")
        checks["same_conversation"] = len({r["session_id"] for r in runs}) == 1
        checks["separate_turns"] = len({r["run_id"] for r in runs}) == len(definition.turns)
        if scenario == "delivery-unknown":
            checks.update(unknown_reported=any(r.get("stage") == "delivery_unknown" for r in receipts),
                no_false_ack=not any(r.get("stage") == "provider_acknowledged" for r in receipts),
                no_replay=len(actions) == 1)
        else:
            if any(r.get("state") in {"accepted", "running", "recovery_required"} for r in runs):
                raise unavailable("Gateway execution outcome is unknown", code="evidence_missing")
            checks["gateway_completed"] = all(r.get("state") == "completed" for r in runs)
            if state["application"] == "live2d":
                checks["client_received_original_result"] = all(
                    r.get("client_reply") == {"session_id": r["session_id"], "run_id": r["run_id"],
                        "state": r["state"], "text": r.get("display_text")} for r in runs)
            if scenario == "dialogue":
                expected = definition.scenario_params["expected_text"]
                checks["agent_output"] = expected in observation.final_text
                if not checks["agent_output"]:
                    failure_kind = "agent_error"
            if state["application"] == "qq":
                if any(r.get("stage") == "delivery_unknown" for r in receipts):
                    raise unavailable("QQ delivery has no confirmed outcome", code="evidence_missing")
                acknowledgements = [r for r in receipts if r.get("stage") == "provider_acknowledged"]
                checks["provider_received"] = len(actions) == len(runs)
                checks["bound_acknowledgements"] = (
                    len(acknowledgements) == len(runs)
                    and {r.get("run_id") for r in acknowledgements} == {r["run_id"] for r in runs}
                    and all(r.get("provider_message_id") for r in acknowledgements)
                )
                delivered = "".join(segment.get("data", {}).get("text", "")
                    for action in actions[-1:] for segment in action.get("params", {}).get("message", [])
                    if segment.get("type") == "text")
                checks["provider_output"] = bool(actions) and observation.final_text in delivered
            if scenario == "session-isolation":
                checks["other_client_denied"] = state.get("scope_denied") is True
            if scenario == "avatar-action":
                motions = [a for a in state.get("avatar_actions", []) if a.get("kind") == "motion"]
                calls = [c for c in observation.tool_calls if c.get("name", "").endswith("play_motion")]
                if motions and not calls:
                    raise unavailable("Avatar effect is not bound to a tool invocation", code="evidence_missing")
                if not calls or not any(c.get("arguments", {}).get("name") == "greet" for c in calls):
                    checks["agent_requested_motion"] = False
                    failure_kind = "agent_error"
                else:
                    checks["one_motion"] = len(motions) == 1
                    checks["configured_motion"] = bool(motions) and motions[0].get("value") == {"group": "TapBody", "index": 0}
                    acks = state.get("avatar_acknowledgements", [])
                    if motions and not acks:
                        raise unavailable("Avatar action lacks a browser receipt", code="evidence_missing")
                    checks["bound_browser_receipt"] = bool(motions) and any(
                        a.get("request_id") == motions[0].get("request_id") and a.get("matched") is True
                        and a.get("status") == "executed" for a in acks)
    failed = tuple(name for name, value in checks.items() if not value)
    return AssertionOutcome(not failed, reasons=("bridge facts verified",) if not failed else failed,
                            violations=failed, checks={"facts": checks, "failure_kind": failure_kind if failed else ""})


def assess(definition, observation):
    outcome = verify(definition, definition.assertions[0], observation)
    return JudgeResult(1.0 if outcome.passed else 0.0, 1.0, outcome.passed,
                       reasons=outcome.reasons, missing=outcome.missing, violations=outcome.violations), {
        "judge_kind": "deterministic:application-bridge", "checks": dict(outcome.checks),
        "failure_kind": outcome.checks["failure_kind"], "quality_applicable": False,
        "quality_reason": "Only frozen execution and bridge facts are graded; no open-ended semantic score.",
        "production_delivery": False,
    }
