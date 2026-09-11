import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from chatcopilot.evals.application import EvaluationApplication
from chatcopilot.evals.application.result_store import EvaluationResultStore


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


def test_case_instance_ids_are_unique_stable_and_do_not_rewrite_results(tmp_path):
    application = EvaluationApplication(root=tmp_path / "evaluations", repository_root=tmp_path)
    first, second = seed(application, 1), seed(application, 2)
    file = application.root / first / "result.json"
    raw = json.loads(file.read_text())
    trial = raw["trials"][0]
    raw["trials"].extend([
        {**trial, "trial_id": "another-target", "target_id": "other"},
        {**trial, "trial_id": "another-attempt", "attempt": 2, "outcome": "failed"},
    ])
    file.write_text(json.dumps(raw))
    before = {p: p.read_bytes() for p in (application.root / first).iterdir() if p.is_file()}
    full = application.get(first)["result"]["trials"]
    ids = [item["case_instance_id"] for item in full]
    assert len(set(ids)) == 4 and all(value.startswith("case-") and len(value) == 37 for value in ids)
    assert not set(ids) & {item["case_instance_id"] for item in application.get(second)["result"]["trials"]}
    assert [item["case_instance_id"] for item in application.get(first, include_bodies=False)["result"]["trials"]] == ids
    application = EvaluationApplication(root=application.root, repository_root=tmp_path)
    instance = application.case_instance(ids[-1])
    assert instance["evaluation_id"] == first and instance["attempt"] == 2
    assert instance["trial"]["outcome"] == "failed"
    assert {p: p.read_bytes() for p in before} == before
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(value["case_instance_id"] == ids[-1] for value in pool.map(application.case_instance, [ids[-1]] * 8))
    application.delete(first)
    with pytest.raises(KeyError):
        application.case_instance(ids[-1])


def test_case_instance_index_upgrades_without_importing_or_changing_results(tmp_path):
    application = EvaluationApplication(root=tmp_path / "evaluations", repository_root=tmp_path)
    identifier = seed(application)
    original = json.loads((application.root / identifier / "result.json").read_text())
    request = json.loads((application.root / identifier / "request.json").read_text())
    store = application.result_store
    store.register(request)
    store.synchronize(identifier, result=original, state={"status": "completed"})
    with store.database.connect(write=True) as connection:
        connection.execute("DROP TABLE case_instances")
    application.result_store = EvaluationResultStore(application.root)
    instance_id = application.get(identifier)["result"]["trials"][0]["case_instance_id"]
    assert application.result_store.get(identifier)["result"] == original
    assert application.case_instance(instance_id)["trial"]["case_id"] == "a"
    legacy = seed(application, 2)
    assert application.get(legacy)["result"]["trials"][0]["case_instance_id"]
    assert application.result_store.get(legacy) is None


def test_case_instance_lookup_revalidates_source_and_rejects_fabricated_ids(tmp_path):
    application = EvaluationApplication(root=tmp_path / "evaluations", repository_root=tmp_path)
    identifier = seed(application)
    instance_id = application.get(identifier)["result"]["trials"][0]["case_instance_id"]
    with pytest.raises(ValueError):
        application.case_instance(identifier)
    with pytest.raises(KeyError):
        application.case_instance("case-" + "0" * 32)
    file = application.root / identifier / "result.json"
    value = json.loads(file.read_text())
    value["trials"][0]["trial_id"] = "changed-trial"
    file.write_text(json.dumps(value))
    with pytest.raises(KeyError):
        application.case_instance(instance_id)
    with pytest.raises(ValueError, match="identity changed"):
        application.get(identifier)


def test_case_instance_index_outage_preserves_results_without_uncopyable_ids(tmp_path, monkeypatch):
    application = EvaluationApplication(root=tmp_path / "evaluations", repository_root=tmp_path)
    identifier = seed(application)
    def unavailable(*args):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(application.result_store, "identify_trials", unavailable)
    record = application.get(identifier)
    assert record["status"] == "completed"
    assert record["result"]["trials"][0]["outcome"] == "passed"
    assert "case_instance_id" not in record["result"]["trials"][0]
