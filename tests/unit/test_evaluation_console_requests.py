from __future__ import annotations

import pytest
from pydantic import ValidationError

from console.backend.routes.evaluations import SuiteEvaluationRequest


def test_suite_request_keeps_legacy_case_selection_as_custom() -> None:
    request = SuiteEvaluationRequest(
        kind="suite",
        bot_id="sample-bot",
        suite_id="bfcl",
        case_ids=["simple-1"],
    )

    assert request.preset == "custom"
    assert request.repetitions == 1
    assert request.max_wall_seconds == 0
    assert request.seed == 0
    assert request.options == {}
    assert request.confirm_external_write is False


def test_named_suite_preset_owns_case_selection() -> None:
    request = SuiteEvaluationRequest(
        kind="suite",
        bot_id="sample-bot",
        suite_id="agentstrata-capabilities-v1",
        preset="quick",
        case_ids=[],
        repetitions=3,
        max_wall_seconds=900,
        seed=42,
    )

    assert request.preset == "quick"
    assert request.case_ids == []
    assert request.repetitions == 3

    with pytest.raises(ValidationError, match="only accepted with preset=custom"):
        SuiteEvaluationRequest(
            kind="suite",
            bot_id="sample-bot",
            suite_id="agentstrata-capabilities-v1",
            preset="quick",
            case_ids=["dialogue-strict-json"],
        )


def test_full_capability_preset_has_no_external_write_confirmation() -> None:
    request = SuiteEvaluationRequest(
        kind="suite",
        bot_id="sample-bot",
        suite_id="agentstrata-capabilities-v1",
        preset="full",
    )
    assert request.confirm_external_write is False

    # BFF retains the strict field for wire compatibility; Core rejects a true
    # value because platform writes now use `bot external-check`.
    legacy = SuiteEvaluationRequest(
        kind="suite",
        bot_id="sample-bot",
        suite_id="agentstrata-capabilities-v1",
        preset="full",
        confirm_external_write=True,
    )
    assert legacy.confirm_external_write is True

    with pytest.raises(ValidationError, match="preset"):
        SuiteEvaluationRequest(
            kind="suite",
            bot_id="sample-bot",
            suite_id="agentstrata-capabilities-v1",
            preset="qq-live",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confirm_external_write", "true"),
        ("confirm_external_write", "yes"),
        ("confirm_external_write", 1),
        ("dry_run", "false"),
        ("llm_judge", 0),
    ],
)
def test_suite_request_rejects_coerced_boolean_values(field: str, value: object) -> None:
    payload = {
        "kind": "suite",
        "bot_id": "sample-bot",
        "suite_id": "ifeval",
        "case_ids": ["ifeval-json-format"],
        "preset": "custom",
        field: value,
    }

    with pytest.raises(ValidationError, match=field):
        SuiteEvaluationRequest.model_validate(payload)


def test_custom_suite_preset_requires_at_least_one_case() -> None:
    with pytest.raises(ValidationError, match="custom preset requires case_ids"):
        SuiteEvaluationRequest(
            kind="suite",
            bot_id="sample-bot",
            suite_id="bfcl",
            preset="custom",
        )
