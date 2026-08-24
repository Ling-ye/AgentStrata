from __future__ import annotations

import json
from pathlib import Path

import pytest

from chatcopilot.contracts.persona_control import PersonaDraftResult
from chatcopilot.evals import qq_flow_scenarios as qq_scenarios
from chatcopilot.evals.capability_scenarios import (
    CapabilityScenarioContext,
    run_capability_scenario,
)
from chatcopilot.evals.capability_verifiers import judge_capability_trial
from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
from chatcopilot.evals.manifest import load_case_definitions, load_suite_manifest
from chatcopilot.evals.qq_flow_scenarios import run_qq_flow_scenario


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = (
    REPOSITORY_ROOT
    / "src"
    / "chatcopilot"
    / "evals"
    / "suites"
    / "agentstrata-qq-message-flow-v1"
)
BOT_PATH = REPOSITORY_ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"


def _case(case_id: str):
    manifest = load_suite_manifest(SUITE_DIR / "manifest.yaml", suite_dir=SUITE_DIR)
    return next(item for item in load_case_definitions(manifest) if item.case_id == case_id)


def test_owned_roundtrip_traverses_acp_task_and_client_chain_without_private_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_markers = (
        "real-token-must-not-leak",
        "real-owner-must-not-leak",
        "real-user-must-not-leak",
    )
    monkeypatch.setenv("QQ_ACCESS_TOKEN", private_markers[0])
    monkeypatch.setenv("CHATCOPILOT_ADD_OWNER_IDS", private_markers[1])
    monkeypatch.setenv("QQ_ALLOW_FROM", private_markers[2])
    runtime = load_evaluation_runtime(
        str(BOT_PATH),
        load_local_environment=False,
        inherit_environment=False,
    )
    workspace = tmp_path / "case"
    workspace.mkdir(mode=0o700)

    observation = run_qq_flow_scenario(
        _case("qq-synthetic-roundtrip"),
        runtime=runtime,
        workspace_root=workspace,
    )
    receipt = next(item for item in observation.evidence if item["kind"] == "qq_owned_chain")
    judge, _judge_evidence = judge_capability_trial(
        _case("qq-synthetic-roundtrip"),
        observation,
    )

    assert judge.passed is True
    assert receipt["passed"] is True
    assert receipt["role_resolved"] is True
    assert receipt["deterministic_agent_invocation_count"] == 1
    assert receipt["client_session_update_count"] == 1
    assert receipt["prompt_plan_set_count"] == 2
    assert receipt["full_external_e2e"] is False
    assert receipt["external_platform_write"] is False
    assert "role_resolution" in receipt["exercised_layers"]
    assert receipt["stubbed_layers"] == [
        "qq_platform",
        "napcat",
        "cc_connect",
        "agent_model",
    ]
    assert list(workspace.iterdir()) == []
    serialized = json.dumps(observation.evidence, ensure_ascii=False, sort_keys=True)
    assert all(marker not in serialized for marker in private_markers)


@pytest.mark.parametrize(
    "case_id",
    (
        "qq-synthetic-roundtrip",
        "qq-attestation-mismatch-denied",
        "qq-persona-persistence-next-turn",
    ),
)
def test_legacy_capability_registry_rejects_removed_owned_chain_scenarios(
    case_id: str,
) -> None:
    runtime = load_evaluation_runtime(
        str(BOT_PATH),
        load_local_environment=False,
        inherit_environment=False,
    )
    context = CapabilityScenarioContext(
        platform_type=runtime.platform_type,
        env={},
        prompt_profile=runtime.prompt_profile,
    )

    with pytest.raises(ValueError, match="no deterministic capability scenario"):
        run_capability_scenario(_case(case_id), context=context)


def test_attestation_mismatch_records_zero_main_agent_invocations(
    tmp_path: Path,
) -> None:
    case_id = "qq-attestation-mismatch-denied"
    runtime = load_evaluation_runtime(
        str(BOT_PATH),
        load_local_environment=False,
        inherit_environment=False,
    )
    workspace = tmp_path / case_id
    workspace.mkdir(mode=0o700)

    observation = run_qq_flow_scenario(
        _case(case_id),
        runtime=runtime,
        workspace_root=workspace,
    )
    receipt = next(
        item for item in observation.evidence if item["kind"] == "qq_attestation_mismatch"
    )
    judge, _evidence = judge_capability_trial(_case(case_id), observation)

    assert judge.passed is True
    assert receipt["agent_invocation_count"] == 0
    assert receipt["agent_invoked"] is False


def test_persona_roundtrip_uses_main_agent_tool_then_loads_next_host_prompt_plan(
    tmp_path: Path,
) -> None:
    runtime = load_evaluation_runtime(
        str(BOT_PATH),
        load_local_environment=False,
        inherit_environment=False,
    )
    workspace = tmp_path / "persona"
    workspace.mkdir(mode=0o700)

    observation = run_qq_flow_scenario(
        _case("qq-persona-persistence-next-turn"),
        runtime=runtime,
        workspace_root=workspace,
    )
    receipt = next(item for item in observation.evidence if item["kind"] == "qq_persona_flow")
    judge, _evidence = judge_capability_trial(
        _case("qq-persona-persistence-next-turn"),
        observation,
    )

    assert judge.passed is True
    assert receipt["first_turn_role_resolved_owner"] is True
    assert receipt["first_turn_persona_tool_visible"] is True
    assert receipt["first_turn_persona_tool_called"] is True
    assert receipt["first_turn_persona_tool_succeeded"] is True
    assert receipt["first_turn_persona_receipt_committed"] is True
    assert receipt["first_turn_main_agent_invocation_count"] == 1
    assert receipt["first_turn_model_replaced"] is True
    assert receipt["first_turn_synthetic_tool_call"] is True
    assert receipt["persona_draft_stub_invocation_count"] == 1
    assert receipt["mutation_receipt_hash_matches_snapshot"] is True
    assert receipt["protected_state_observed"] is True
    assert receipt["fresh_acp_host_count"] == 2
    assert receipt["next_turn_prompt_persona_layer_count"] == 1
    assert receipt["next_turn_prompt_contains_marker"] is True
    assert receipt["next_turn_main_agent_invocation_count"] == 1
    assert receipt["next_turn_client_received_sentinel"] is True
    assert receipt["full_external_e2e"] is False
    assert list(workspace.iterdir()) == []


def test_persona_draft_failure_preserves_old_hash_and_cannot_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingDraftFactory:
        def __init__(self, _marker: str) -> None:
            self.construction_count = 0
            self.draft_call_count = 0
            self.requests: list[dict[str, object]] = []
            self.last_markdown = ""

        def __call__(self, **_kwargs: object) -> _FailingDraftFactory:
            self.construction_count += 1
            return self

        def draft(self, **kwargs: object) -> PersonaDraftResult:
            self.draft_call_count += 1
            self.requests.append(dict(kwargs))
            return PersonaDraftResult(error_code="deterministic_persona_draft_failure")

    monkeypatch.setattr(
        qq_scenarios,
        "_DeterministicPersonaDraftFactory",
        _FailingDraftFactory,
    )
    runtime = load_evaluation_runtime(
        str(BOT_PATH),
        load_local_environment=False,
        inherit_environment=False,
    )
    workspace = tmp_path / "persona-failure"
    workspace.mkdir(mode=0o700)

    observation = run_qq_flow_scenario(
        _case("qq-persona-persistence-next-turn"),
        runtime=runtime,
        workspace_root=workspace,
    )
    receipt = next(item for item in observation.evidence if item["kind"] == "qq_persona_flow")
    judge, _evidence = judge_capability_trial(
        _case("qq-persona-persistence-next-turn"),
        observation,
    )

    assert judge.passed is False
    assert receipt["passed"] is False
    assert receipt["initial_persona_hash"] == receipt["persisted_persona_hash"]
    assert receipt["first_turn_main_agent_invocation_count"] == 1
    assert receipt["first_turn_persona_tool_succeeded"] is False
    assert receipt["first_turn_persona_receipt_committed"] is False
    assert receipt["next_turn_prompt_persona_layer_count"] == 0
    assert receipt["next_turn_prompt_contains_marker"] is False
    assert list(workspace.iterdir()) == []
