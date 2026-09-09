import json
from datetime import datetime, timedelta, timezone

import pytest

from chatcopilot.evals.application import EvaluationApplication


def seed(application, number=1, *, bot="bot-a"):
    identifier = f"eval-read-{number:04d}"
    directory = application.root / identifier
    directory.mkdir(mode=0o700)
    started = (datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=number)).isoformat()
    request = {
        "evaluation_id": identifier,
        "kind": "suite",
        "bot_id": bot,
        "suite_id": "fixture-suite",
        "case_ids": ["a", "b"],
        "repetitions": 1,
        "seed": 0,
        "max_wall_seconds": 600,
        "created_at": started,
    }
    result = {
        "evaluation_id": identifier,
        "kind": "suite",
        "suite": "fixture-suite",
        "started_at": started,
        "selected_cases": ["fixture-suite:a", "fixture-suite:b"],
        "repetitions": 1,
        "targets": [
            {
                "target_id": "main",
                "executor": "agent_configured",
                "backend": "native",
                "model": "controlled",
            }
        ],
        "config_snapshot": {
            "case_hash": "cases",
            "judge": "deepeval",
            "definition_snapshot": {
                "manifest": {"suite_id": "fixture-suite"},
                "protocols": {"scorer": "1"},
                "execution_implementations": {"engine": "sdk"},
            },
        },
        "trials": [
            {
                "evaluation_id": identifier,
                "trial_id": f"{identifier}-{case}",
                "case_id": case,
                "case_ref": f"fixture-suite:{case}",
                "target_id": "main",
                "attempt": 1,
                "outcome": "passed",
                "final_text": "final answer" * 1000,
                "evidence": {
                    "execution": {
                        "state": "recorded",
                        "turns": [
                            {
                                "input": "effective input " + case,
                                "final_text": "final answer",
                                "conversation_id": "actor",
                                "turn_index": 0,
                            }
                        ],
                    }
                },
            }
            for case in ("a", "b")
        ],
    }
    for name, value in (
        ("request.json", request),
        ("state.json", {"evaluation_id": identifier, "status": "completed", "started_at": started}),
        ("result.json", result),
    ):
        path = directory / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    return identifier


def test_history_is_paginated_across_bots_without_reading_bodies(tmp_path):
    application = EvaluationApplication(root=tmp_path / "evaluations", repository_root=tmp_path)
    for i in range(1, 61):
        seed(application, i, bot="bot-a" if i % 2 else "bot-b")
    first = application.list(limit=50)
    second = application.list(offset=50, limit=50)
    assert len(first) == 50 and len(second) == 10
    assert not set(r["evaluation_id"] for r in first) & set(r["evaluation_id"] for r in second)
    assert all("result" not in r for r in first)
    assert (
        len(
            application.list(
                bot_ids=["bot-a", "bot-b"], since="2026-09-03T00:00:00+00:00", limit=50
            )
        )
        == 13
    )
    assert all(r["bot_id"] == "bot-b" for r in application.list(bot_ids=["bot-b"]))


def test_preview_and_precise_body_read_are_bound_to_trial(tmp_path):
    application = EvaluationApplication(root=tmp_path / "evaluations", repository_root=tmp_path)
    identifier = seed(application)
    value = application.get(identifier, include_bodies=False)
    assert value["result"]["trials"][0]["input_preview"] == "effective input a"
    assert len(value["result"]["trials"][0]["final_text"]) == 400
    detail = application.case_detail(
        identifier, "fixture-suite:a", trial_id=f"{identifier}-a", target_id="main", attempt=1
    )
    assert len(detail["trials"]) == 1
    assert len(detail["trials"][0]["final_text"]) > 400
    with pytest.raises(KeyError):
        application.case_detail(identifier, "fixture-suite:b", trial_id=f"{identifier}-a")
    with pytest.raises(KeyError):
        application.case_detail(identifier, "fixture-suite:a", target_id="other")


def test_unfinished_execution_is_readable_without_entering_trial_counts(tmp_path):
    application = EvaluationApplication(root=tmp_path / "evaluations", repository_root=tmp_path)
    identifier = seed(application)
    path = application.root / identifier / "observation.json"
    payload = {
        "evaluation_id": identifier,
        "trial_id": "unfinished",
        "case_id": "pending",
        "target_id": "main",
        "attempt": 1,
        "execution": {"turns": [{"input": "actual", "final_text": "already generated"}]},
    }
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    detail = application.case_detail(identifier, "pending", trial_id="unfinished")
    assert detail["trials"] == []
    assert (
        detail["execution_observation"]["execution"]["turns"][0]["final_text"]
        == "already generated"
    )
    assert application.get(identifier)["insights"]["observed"] == 2
    payload["evaluation_id"] = "other"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="identity"):
        application.get(identifier)
