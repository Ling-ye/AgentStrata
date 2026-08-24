from __future__ import annotations

from pathlib import Path

import pytest

from chatcopilot.contracts.identity import Identity
from chatcopilot.evals.capability_scenarios import (
    CapabilityScenarioContext,
    run_capability_scenario,
    run_group_unknown_identity_scenario,
)
from chatcopilot.evals.capability_verifiers import judge_capability_trial
from chatcopilot.evals.manifest import load_case_definitions, load_suite_manifest


SUITE_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "chatcopilot"
    / "evals"
    / "suites"
    / "agentstrata-qq-message-flow-v1"
)
AGENT_SUITE_DIR = SUITE_DIR.parent / "agentstrata-capabilities-v1"


def _case(case_id: str):
    manifest = load_suite_manifest(SUITE_DIR / "manifest.yaml", suite_dir=SUITE_DIR)
    return next(case for case in load_case_definitions(manifest) if case.case_id == case_id)


def _agent_case(case_id: str):
    manifest = load_suite_manifest(
        AGENT_SUITE_DIR / "manifest.yaml",
        suite_dir=AGENT_SUITE_DIR,
    )
    return next(case for case in load_case_definitions(manifest) if case.case_id == case_id)


def _context(monkeypatch: pytest.MonkeyPatch) -> CapabilityScenarioContext:
    monkeypatch.setenv("QQ_ALLOW_FROM", "10002,10001")
    monkeypatch.setenv("QQ_ALLOW_GROUPS", "10004")
    monkeypatch.setenv("CHATCOPILOT_ADD_OWNER_IDS", "10001")
    monkeypatch.delenv("CHATCOPILOT_ADD_OWNER_NAMES", raising=False)
    monkeypatch.delenv("CHATCOPILOT_ADD_ADMIN_IDS", raising=False)
    monkeypatch.delenv("CHATCOPILOT_ADD_ADMIN_NAMES", raising=False)
    return CapabilityScenarioContext(
        platform_type="qq",
        env={
            "QQ_ALLOW_FROM": "10002,10001",
            "QQ_ALLOW_GROUPS": "10004",
            "QQ_ACCOUNT": "10003",
        },
        owners=(Identity(user_id="10001"),),
        group_id="10004",
    )


def test_member_owner_action_runs_selected_gate_then_denies_by_stable_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case("qq-member-owner-action-denied")

    observation = run_capability_scenario(case, context=_context(monkeypatch))
    decision = observation.evidence[0]
    execution = observation.evidence[1]
    matrix = observation.evidence[2]
    judge, _evidence = judge_capability_trial(case, observation)

    assert decision["kind"] == "access_decision"
    assert decision["selected_bot_policy"] is True
    assert decision["gate_allowed"] is True
    assert decision["gate_reason"] == "qq-group-user-allowed"
    assert decision["resolved_role"] == "user"
    assert decision["action_authorized"] is False
    assert execution == {
        "kind": "owner_tool_execution_denial",
        "production_permission_filter_exercised": True,
        "execution_path": "ToolExecutor.execute",
        "executor_class": "ToolExecutor",
        "tool_requires_role": "owner",
        "caller_role": "user",
        "schema_hidden": True,
        "permission_filter_denied": True,
        "crafted_call_executed": True,
        "result_ok": False,
        "result_error_present": True,
        "handler_invocation_count": 0,
    }
    assert matrix["kind"] == "access_matrix"
    assert matrix["production_qq_relay_exercised"] is True
    assert matrix["production_acp_admission_exercised"] is True
    assert matrix["relay_allowlist_read"] is False
    assert matrix["relay_fixed_group_mention_trigger"] is True
    assert matrix["all_expected"] is True
    assert {row["scenario"] for row in matrix["rows"]} == {
        "private_allowlisted",
        "private_group_only_member",
        "group_only_member_without_at",
        "group_only_member_with_at",
        "other_group_with_at",
        "group_unknown_identity_with_at",
    }
    assert matrix["session_created"] is False
    assert matrix["tool_invocation_count"] == 0
    assert matrix["platform_write_count"] == 0
    assert observation.post_state["sentinel_before"] == observation.post_state["sentinel_after"]
    assert observation.post_state["mutation_count"] == 0
    assert judge.passed is True


def test_qq_nickname_spoof_cannot_replace_stable_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case("qq-nickname-spoof-denied")

    observation = run_capability_scenario(case, context=_context(monkeypatch))
    decision = observation.evidence[0]
    judge, _evidence = judge_capability_trial(case, observation)

    assert decision["kind"] == "identity_decision"
    assert decision["gate_allowed"] is True
    assert decision["allow_name_match"] is False
    assert decision["display_names_equal"] is True
    assert decision["stable_ids_distinct"] is True
    assert decision["resolved_role"] == "user"
    assert decision["action_authorized"] is False
    assert observation.post_state["mutation_count"] == 0
    assert judge.passed is True


def test_group_mention_with_unknown_identity_is_denied_before_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = run_group_unknown_identity_scenario(_context(monkeypatch))
    decision = observation.evidence[0]

    assert decision["kind"] == "access_decision"
    assert decision["chat_kind"] == "group"
    assert decision["stable_user_id_present"] is False
    assert decision["gate_allowed"] is False
    assert decision["gate_reason"] == "qq-sender-invalid"
    assert observation.stop_reason == "access_denied"
    assert observation.post_state["sentinel_before"] == observation.post_state["sentinel_after"]
    assert observation.post_state["mutation_count"] == 0


def test_remote_reference_uses_production_attachment_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case("qq-remote-url-not-attachment")

    observation = run_capability_scenario(case, context=_context(monkeypatch))
    boundary = observation.evidence[0]
    judge, _evidence = judge_capability_trial(case, observation)

    assert boundary["kind"] == "remote_reference_boundary"
    assert boundary["production_parser_exercised"] is True
    assert boundary["classified_as_local"] is False
    assert boundary["local_candidate_count"] == 0
    assert boundary["local_read_attempted"] is False
    assert observation.post_state["mutation_count"] == 0
    assert judge.passed is True


def test_scenario_registry_rejects_non_scenario_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="no deterministic capability scenario"):
        run_capability_scenario(
            _agent_case("dialogue-strict-json"),
            context=_context(monkeypatch),
        )
