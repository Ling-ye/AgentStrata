from __future__ import annotations

from collections import Counter
from pathlib import Path

from chatcopilot.evals.capability_verifiers import judge_capability_trial
from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
from chatcopilot.evals.manifest import load_case_definitions, load_suite_manifest
from chatcopilot.evals.qq_flow_scenarios import run_qq_flow_scenario
from chatcopilot.evals.suite_loader import load_suite_cases


SUITE_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "chatcopilot"
    / "evals"
    / "suites"
    / "agentstrata-qq-message-flow-v1"
)
BOT_PATH = Path(__file__).resolve().parents[2] / "bots" / "lingye-copilot-qq" / "bot.yaml"


def test_qq_message_flow_suite_has_one_explicit_track_and_seven_cases() -> None:
    manifest = load_suite_manifest(SUITE_DIR / "manifest.yaml", suite_dir=SUITE_DIR)
    cases = load_suite_cases(manifest.suite_id, manifest=manifest)
    definitions = load_case_definitions(manifest)

    assert manifest.track == "qq_message_flow"
    assert manifest.plugin_id == "qq-message-flow"
    assert manifest.driver_id == "qq_message_flow"
    assert len(cases) == len(definitions) == 7
    assert {item.plugin_id for item in definitions} == {"qq-message-flow"}
    assert {item.driver_id for item in definitions} == {"qq_message_flow"}
    assert all(item.policy.side_effect != "external_write" for item in definitions)
    assert Counter(item.capability for item in definitions) == {"qq_message_flow": 7}
    assert len(next(item for item in manifest.presets if item.preset_id == "quick").case_ids) == 3
    assert len(next(item for item in manifest.presets if item.preset_id == "full").case_ids) == 7
    assert len(next(item for item in manifest.presets if item.preset_id == "security").case_ids) == 4


def test_every_qq_message_flow_scenario_produces_a_passing_structured_receipt(
    tmp_path: Path,
) -> None:
    manifest = load_suite_manifest(SUITE_DIR / "manifest.yaml", suite_dir=SUITE_DIR)
    runtime = load_evaluation_runtime(
        str(BOT_PATH),
        load_local_environment=False,
        inherit_environment=False,
    )

    for definition in load_case_definitions(manifest):
        workspace = tmp_path / definition.case_id
        workspace.mkdir(mode=0o700)
        observation = run_qq_flow_scenario(
            definition,
            runtime=runtime,
            workspace_root=workspace,
        )
        judge, evidence = judge_capability_trial(definition, observation)

        assert judge.passed is True, (definition.case_id, evidence)
        assert observation.post_state["sentinel_before"] == observation.post_state["sentinel_after"]
        assert all(item.get("external_platform_write") is not True for item in observation.evidence)


def test_positive_qq_flow_names_exercised_stubbed_and_excluded_layers(
    tmp_path: Path,
) -> None:
    manifest = load_suite_manifest(SUITE_DIR / "manifest.yaml", suite_dir=SUITE_DIR)
    definition = next(
        item for item in load_case_definitions(manifest) if item.case_id == "qq-synthetic-roundtrip"
    )

    runtime = load_evaluation_runtime(
        str(BOT_PATH),
        load_local_environment=False,
        inherit_environment=False,
    )
    workspace = tmp_path / definition.case_id
    workspace.mkdir(mode=0o700)
    observation = run_qq_flow_scenario(
        definition,
        runtime=runtime,
        workspace_root=workspace,
    )
    receipt = next(item for item in observation.evidence if item.get("kind") == "qq_owned_chain")

    assert receipt["passed"] is True
    assert receipt["external_platform_write"] is False
    assert set(receipt["stubbed_layers"]) == {
        "qq_platform",
        "napcat",
        "cc_connect",
        "agent_model",
    }
    assert receipt["excluded_layers"] == ["external_qq_write"]
    assert {
        "access_proxy",
        "ingress_receipt",
        "session_attestation_writer",
        "acp_chat_agent",
        "turn_orchestrator",
        "task_observability",
        "role_resolution",
        "prompt_plan",
        "agent_task_contract",
        "event_translator",
        "acp_client_delivery",
    }.issubset(set(receipt["exercised_layers"]))
