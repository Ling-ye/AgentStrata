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
    ("path", "category", "preset", "expected_case"),
    (
        (
            "src/chatcopilot/agent/session.py",
            "agent",
            "full",
            "subagent-structured-result",
        ),
        (
            "src/chatcopilot/core/llm_client.py",
            "runtime",
            "full",
            "session-cross-user-isolation",
        ),
        (
            "src/chatcopilot/external_tools/wiki/service.py",
            "tools",
            "full",
            "tool-disabled-hidden-no-effect",
        ),
        (
            "src/chatcopilot/agent/search/coordinator.py",
            "search",
            "full",
            "search-conflict-disclosure",
        ),
        (
            "src/chatcopilot/middleware/acp/image_pipeline.py",
            "multimodal",
            "full",
            "image-multi-input-order",
        ),
        (
            "src/chatcopilot/core/workspace_runtime/resolver.py",
            "workspace",
            "full",
            "workspace-write-contained",
        ),
        (
            "src/chatcopilot/middleware/acp/turn_pipeline.py",
            "acp",
            "full",
            "session-same-user-memory",
        ),
        (
            "src/chatcopilot/middleware/acp/access_gate.py",
            "access",
            "security",
            "access-nickname-spoof-denied",
        ),
        (
            "src/chatcopilot/platforms/qq/adapter.py",
            "qq",
            None,
            None,
        ),
        (
            "src/chatcopilot/external_tools/dev/code_task_service.py",
            "code-task",
            "full",
            "code-restart-and-health",
        ),
        (
            "src/chatcopilot/evals/manifest.py",
            "evals",
            "quick",
            "dialogue-strict-json",
        ),
        (
            "docs/unclassified-change.md",
            "unknown",
            "quick",
            "dialogue-strict-json",
        ),
    ),
)
def test_changed_path_maps_to_validated_capability_advice(
    path: str,
    category: str,
    preset: str | None,
    expected_case: str | None,
) -> None:
    result = advise_capability_evaluation((path,))

    assert isinstance(result, EvaluationAdvice)
    assert result.changed_paths == (path,)
    assert result.categories == (category,)
    assert result.recommended_preset == preset
    if expected_case is None:
        assert result.case_ids == ()
        assert result.external_checks == ("qq",)
    else:
        assert expected_case in result.case_ids
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
    assert first.categories == ("search", "qq")
    assert first.recommended_preset == "full"
    assert "search-explicit-source" in first.case_ids
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
            "src/chatcopilot/middleware/acp/access_gate.py",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["recommended_preset"] == "security"
    assert payload["external_checks"] == []
    assert payload["manual_only"] is True


def test_cli_advisor_reports_qq_as_external_check_not_agent_case(
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
    assert payload["recommended_preset"] is None
    assert payload["case_ids"] == []
    assert payload["external_checks"] == ["qq"]


def test_cli_advisor_rejects_unsafe_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = evals_cli_main(["advise", "--changed-path", "../outside.py", "--json"])

    payload = json.loads(capsys.readouterr().err)
    assert code == 2
    assert payload["code"] == "invalid_changed_paths"
