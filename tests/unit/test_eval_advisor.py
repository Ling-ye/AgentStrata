from __future__ import annotations

import json
from dataclasses import replace

import pytest

from chatcopilot.evals import advisor
from chatcopilot.evals.advisor import EvaluationAdvice, advise_capability_evaluation
from chatcopilot.evals.cli import main as evals_cli_main
from chatcopilot.evals.manifest import discover_suite_manifests


_MACHINE_ABSOLUTE_PATH = "/" + "/".join(
    ("home", "user", "repository", "file.py")
)


@pytest.mark.parametrize(
    ("path", "expected_runs", "external_checks"),
    (
        (
            "src/chatcopilot/agent/session.py",
            {"agent": ("full", "subagent-structured-result")},
            (),
        ),
        (
            "src/chatcopilot/core/llm_client.py",
            {"agent": ("full", "session-cross-user-isolation")},
            (),
        ),
        (
            "src/chatcopilot/external_tools/wiki/service.py",
            {"agent": ("full", "tool-disabled-hidden-no-effect")},
            (),
        ),
        (
            "src/chatcopilot/agent/search/coordinator.py",
            {"agent": ("full", "search-general-with-evidence")},
            (),
        ),
        (
            "src/chatcopilot/core/image_content.py",
            {"agent": ("full", "image-multi-input-order")},
            (),
        ),
        (
            "src/chatcopilot/core/workspace_runtime/resolver.py",
            {"agent": ("full", "workspace-write-contained")},
            (),
        ),
        (
            "src/chatcopilot/middleware/acp/turn_pipeline.py",
            {"qq_message_flow": ("full", "qq-synthetic-roundtrip")},
            (),
        ),
            (
                "src/chatcopilot/middleware/acp/admission.py",
                {"qq_message_flow": ("security", "qq-nickname-spoof-denied")},
            (),
        ),
        (
            "src/chatcopilot/platforms/qq/adapter.py",
            {"qq_message_flow": ("full", "qq-synthetic-roundtrip")},
            ("qq",),
        ),
        (
            "src/chatcopilot/external_tools/dev/code_task_service.py",
            {"agent": ("full", "code-restart-and-health")},
            (),
        ),
        (
            "src/chatcopilot/evals/manifest.py",
            {
                "agent": ("quick", "dialogue-strict-json"),
                "qq_message_flow": ("quick", "qq-synthetic-roundtrip"),
            },
            (),
        ),
        (
            "docs/unclassified-change.md",
            {"agent": ("quick", "dialogue-strict-json")},
            (),
        ),
    ),
)
def test_changed_path_maps_to_validated_capability_advice(
    path: str,
    expected_runs: dict[str, tuple[str, str]],
    external_checks: tuple[str, ...],
) -> None:
    result = advise_capability_evaluation((path,))

    assert isinstance(result, EvaluationAdvice)
    assert result.changed_paths == (path,)
    by_track = {run.track: run for run in result.runs}
    assert set(by_track) == set(expected_runs)
    for track, (preset, expected_case) in expected_runs.items():
        assert by_track[track].recommended_preset == preset
        assert expected_case in by_track[track].case_ids
    assert result.external_checks == external_checks
    assert result.reason


def test_unknown_path_uses_exact_current_quick_preset() -> None:
    manifest = next(
        item
        for item in discover_suite_manifests()
        if item.suite_id == "agentstrata-capabilities-v1"
    )
    quick = next(item.case_ids for item in manifest.presets if item.preset_id == "quick")

    result = advise_capability_evaluation(("README.md",))

    assert result.recommended_preset == "quick"
    assert result.case_ids == quick
    assert "保守退回 quick" in result.reason


def test_multiple_categories_are_order_independent_and_recommend_custom() -> None:
    paths = (
        "src/chatcopilot/platforms/qq/adapter.py",
        "src/chatcopilot/agent/search/router.py",
    )

    first = advise_capability_evaluation(paths)
    second = advise_capability_evaluation(reversed(paths))

    assert first == second
    assert first.categories == ("search", "qq-message-flow", "qq")
    by_track = {run.track: run for run in first.runs}
    assert by_track["agent"].recommended_preset == "full"
    assert "search-general-with-evidence" in by_track["agent"].case_ids
    assert "search-explicit-source" not in by_track["agent"].case_ids
    assert "search-conflict-disclosure" not in by_track["agent"].case_ids
    assert "current-usd-cny-reference" in by_track["agent"].case_ids
    assert by_track["qq_message_flow"].recommended_preset == "full"
    assert first.external_checks == ("qq",)


@pytest.mark.parametrize(
    "path",
    (
        _MACHINE_ABSOLUTE_PATH,
        "../outside.py",
        "src/chatcopilot/../../outside.py",
        r"C:\repository\file.py",
    ),
)
def test_unsafe_changed_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        advise_capability_evaluation((path,))


def test_empty_changed_path_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        advise_capability_evaluation(())


def test_rule_case_drift_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    search_rule = advisor._RULES[0]
    monkeypatch.setattr(
        advisor,
        "_RULES",
        (replace(search_rule, case_ids=("removed-case",)), *advisor._RULES[1:]),
    )

    with pytest.raises(RuntimeError, match="unknown capability cases"):
        advise_capability_evaluation(("src/chatcopilot/agent/search/router.py",))


def test_dot_prefix_is_normalized_and_duplicates_are_deduplicated() -> None:
    result = advise_capability_evaluation(
        (
            "./src/chatcopilot/agent/session.py",
            "src/chatcopilot/agent/session.py",
        )
    )

    assert result.changed_paths == ("src/chatcopilot/agent/session.py",)


def test_cli_advisor_is_read_only_and_returns_manual_recommendation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = evals_cli_main(
        [
            "advise",
            "--changed-path",
            "src/chatcopilot/middleware/acp/admission.py",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["recommended_preset"] == "security"
    assert {item["track"] for item in payload["runs"]} == {"qq_message_flow"}
    assert {item["recommended_preset"] for item in payload["runs"]} == {"security"}
    assert payload["external_checks"] == []
    assert payload["manual_only"] is True


def test_cli_advisor_reports_qq_flow_and_separate_external_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = evals_cli_main(
        [
            "advise",
            "--changed-path",
            "src/chatcopilot/platforms/qq/gateway_health.py",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["runs"][0]["suite_id"] == "agentstrata-qq-message-flow-v1"
    assert payload["runs"][0]["recommended_preset"] == "full"
    assert payload["external_checks"] == ["qq"]


def test_cli_advisor_rejects_unsafe_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = evals_cli_main(["advise", "--changed-path", "../outside.py", "--json"])

    payload = json.loads(capsys.readouterr().err)
    assert code == 2
    assert payload["code"] == "invalid_changed_paths"
