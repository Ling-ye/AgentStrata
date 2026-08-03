from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from console.backend.app import app
from console.control.discovery import repo_root
from console.control.evaluations import (
    EvaluationBlocked,
    EvaluationManager,
)
from console.control.instances import BotInstance


def _instance() -> BotInstance:
    return BotInstance(
        instance_id="lingye-copilot-qq",
        bot_spec="bots/lingye-copilot-qq/bot.yaml",
        display_name="Lingye Copilot QQ",
        platform="qq",
    )


def _ready_validator(
    captured: list[dict[str, Any]] | None = None,
) -> Callable[
    [BotInstance, Mapping[str, Any]],
    Mapping[str, Any],
]:
    def validate(
        _instance_value: BotInstance,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if captured is not None:
            captured.append(dict(request))
        if request["kind"] == "comparison":
            return {
                "ready": True,
                "checks": [{"code": "bot", "ok": True}],
                "effective_request": {
                    "profile": request.get(
                        "profile_id",
                        "agent-comparison-mvp",
                    ),
                    "preset": request.get("preset", "quick"),
                    "targets": ["codex", "native"],
                    "case_refs": [
                        "ifeval:ifeval-json-format",
                        "bfcl:bfcl-simple-weather",
                    ],
                    "repetitions": 1,
                    "max_wall_seconds": 900,
                    "seed": 7,
                },
                "targets": [
                    {
                        "target_id": "codex",
                        "executor": "agent",
                        "backend": "codex",
                        "model": "gpt-5",
                        "reasoning_effort": "high",
                        "fingerprint": "target-codex-v1",
                    },
                    {
                        "target_id": "native",
                        "executor": "agent",
                        "backend": "native",
                        "model": "gpt-5",
                        "reasoning_effort": "high",
                        "fingerprint": "target-native-v1",
                    },
                ],
            }
        return {
            "ready": True,
            "checks": [{"code": "suite", "ok": True}],
            "effective_request": {
                "suite": request["suite_id"],
                "case_ids": list(request["case_ids"]),
                "dry_run": bool(request.get("dry_run", False)),
                "llm_judge": bool(request.get("llm_judge", False)),
            },
            "targets": [
                {
                    "target_id": "dry-run",
                    "executor": "dry-run",
                    "backend": "none",
                    "model": "",
                    "reasoning_effort": "",
                    "fingerprint": "target-dry-run-v1",
                }
            ],
        }

    return validate


@contextmanager
def _use_manager(manager: EvaluationManager) -> Iterator[None]:
    previous = app.state.evaluations
    app.state.evaluations = manager
    try:
        yield
    finally:
        app.state.evaluations = previous


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _persist_evaluation(
    root: Path,
    *,
    evaluation_id: str,
    kind: str = "comparison",
    bot_id: str = "lingye-copilot-qq",
    lifecycle_status: str = "completed",
    trials: list[dict[str, Any]] | None = None,
) -> None:
    directory = root / evaluation_id
    created_at = "2026-07-26T00:00:00+00:00"
    request: dict[str, Any] = {
        "evaluation_id": evaluation_id,
        "kind": kind,
        "bot_id": bot_id,
        "bot_spec": "bots/lingye-copilot-qq/bot.yaml",
        "created_at": created_at,
        "targets": [],
    }
    if kind == "comparison":
        request.update(
            {
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
                "target_ids": ["codex", "native"],
                "case_refs": ["ifeval:ifeval-json-format"],
                "repetitions": 1,
                "max_wall_seconds": 900,
                "seed": 7,
            }
        )
    else:
        request.update(
            {
                "suite_id": "ifeval",
                "case_ids": ["ifeval-json-format"],
                "dry_run": False,
                "llm_judge": False,
            }
        )
    _write_json(directory / "request.json", request)
    _write_json(
        directory / "state.json",
        {
            "evaluation_id": evaluation_id,
            "kind": kind,
            "status": lifecycle_status,
            "pid": None,
            "started_at": created_at,
            "finished_at": "2026-07-26T00:01:00+00:00",
            "duration_seconds": 60.0,
            "completed_trials": len(trials or []),
            "planned_trials": len(trials or []),
            "error": None,
        },
    )
    _write_json(
        directory / "result.json",
        {
            "evaluation_id": evaluation_id,
            "kind": kind,
            "status": lifecycle_status,
            "trials": trials or [],
            "summary": {},
            "duration_seconds": 60.0,
        },
    )


def _evaluation_directories(root: Path) -> list[Path]:
    return [path for path in root.iterdir() if path.is_dir()]


def _competing_start_process(
    root: str,
    results: Any,
    release: Any,
) -> None:
    manager = EvaluationManager(
        Path(root),
        validator=_ready_validator(),
    )
    try:
        with patch.object(manager, "_spawn"):
            evaluation = manager.start(
                instance=_instance(),
                request={
                    "kind": "comparison",
                    "profile_id": "agent-comparison-mvp",
                    "preset": "quick",
                },
            )
        results.put(("started", evaluation["evaluation_id"]))
        release.wait(timeout=10)
    except RuntimeError as exc:
        results.put(("blocked", str(exc)))


def test_quick_create_uses_server_defaults_without_client_overrides(
    tmp_path: Path,
) -> None:
    captured: list[dict[str, Any]] = []
    manager = EvaluationManager(
        tmp_path / "evaluations",
        validator=_ready_validator(captured),
    )
    with (
        patch.object(manager, "_spawn") as spawn,
        _use_manager(manager),
    ):
        response = TestClient(app).post(
            "/api/evals/evaluations",
            json={
                "kind": "comparison",
                "bot_id": "lingye-copilot-qq",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )

    assert response.status_code == 200
    assert set(captured[0]) == {
        "kind",
        "bot_id",
        "profile_id",
        "preset",
    }
    payload = response.json()
    assert payload["kind"] == "comparison"
    assert payload["status"] == "queued"
    assert payload["request"]["target_ids"] == ["codex", "native"]
    assert payload["progress"] == {
        "completed": 0,
        "total": 4,
        "percent": 0,
    }
    spawn.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [
        {
            "kind": "comparison",
            "bot_id": "lingye-copilot-qq",
            "profile_id": "agent-comparison-mvp",
            "preset": "quick",
            "target_ids": ["codex", "native"],
        },
        {
            "kind": "suite",
            "bot_id": "lingye-copilot-qq",
            "suite_id": "ifeval",
            "case_ids": ["ifeval-json-format"],
            "profile_id": "agent-comparison-mvp",
        },
    ],
)
def test_create_contract_rejects_preset_overrides_and_cross_kind_fields(
    tmp_path: Path,
    body: dict[str, Any],
) -> None:
    manager = EvaluationManager(
        tmp_path / "evaluations",
        validator=_ready_validator(),
    )
    with _use_manager(manager):
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/evals/evaluations",
            json=body,
        )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert not _evaluation_directories(manager.root)


def test_blocking_validation_has_no_record_or_process_side_effect(
    tmp_path: Path,
) -> None:
    def blocked(
        _instance_value: BotInstance,
        _request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "ready": False,
            "message": "Codex CLI 不可用",
            "checks": [
                {
                    "code": "target:codex",
                    "ok": False,
                    "message": "找不到 Codex CLI",
                }
            ],
        }

    manager = EvaluationManager(
        tmp_path / "evaluations",
        validator=blocked,
    )
    with (
        patch.object(manager, "_spawn") as spawn,
        _use_manager(manager),
    ):
        response = TestClient(app).post(
            "/api/evals/evaluations",
            json={
                "kind": "comparison",
                "bot_id": "lingye-copilot-qq",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "evaluation_blocked",
        "message": "Codex CLI 不可用",
        "checks": [
            {
                "code": "target:codex",
                "ok": False,
                "message": "找不到 Codex CLI",
            }
        ],
    }
    assert not _evaluation_directories(manager.root)
    assert not list(manager.root.glob(".active-*.json"))
    spawn.assert_not_called()

    with pytest.raises(EvaluationBlocked):
        manager.start(
            instance=_instance(),
            request={
                "kind": "comparison",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )


def test_startup_failure_is_sanitized_before_any_error_is_persisted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    manager = EvaluationManager(root, validator=_ready_validator())
    secret = "startup-secret-value"
    absolute_path = repo_root() / "private" / "credentials.json"
    raw_error = f"token {secret} failed at {absolute_path}"

    with (
        patch(
            "console.control.evaluations._bot_env",
            return_value={"CUSTOM_TOKEN": secret},
        ),
        patch.object(
            manager,
            "_spawn",
            side_effect=RuntimeError(raw_error),
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        manager.start(
            instance=_instance(),
            request={
                "kind": "comparison",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )

    directories = _evaluation_directories(root)
    assert len(directories) == 1
    state = json.loads(
        (directories[0] / "state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "error"
    assert secret not in state["error"]
    assert str(repo_root()) not in state["error"]
    assert "[REDACTED]" in state["error"]
    assert "$REPOSITORY" in state["error"]
    assert secret not in str(raised.value)
    for path in directories[0].rglob("*"):
        if path.is_file():
            persisted = path.read_text(encoding="utf-8")
            assert secret not in persisted
            assert str(repo_root()) not in persisted
    assert not list(root.glob(".active-*.json"))


def test_same_bot_allows_only_one_active_evaluation(
    tmp_path: Path,
) -> None:
    manager = EvaluationManager(
        tmp_path / "evaluations",
        validator=_ready_validator(),
    )
    body = {
        "kind": "comparison",
        "bot_id": "lingye-copilot-qq",
        "profile_id": "agent-comparison-mvp",
        "preset": "quick",
    }
    with (
        patch.object(manager, "_spawn"),
        _use_manager(manager),
    ):
        client = TestClient(app)
        first = client.post("/api/evals/evaluations", json=body)
        second = client.post("/api/evals/evaluations", json=body)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "active evaluation" in second.json()["detail"]
    assert len(_evaluation_directories(manager.root)) == 1


def test_separate_managers_atomically_claim_one_bot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    first = EvaluationManager(root, validator=_ready_validator())
    second = EvaluationManager(root, validator=_ready_validator())

    def start(manager: EvaluationManager) -> str:
        with patch.object(manager, "_spawn"):
            try:
                manager.start(
                    instance=_instance(),
                    request={
                        "kind": "comparison",
                        "profile_id": "agent-comparison-mvp",
                        "preset": "quick",
                    },
                )
            except RuntimeError:
                return "blocked"
        return "started"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(start, (first, second)))

    assert sorted(outcomes) == ["blocked", "started"]
    assert len(_evaluation_directories(root)) == 1


@pytest.mark.skipif(os.name == "nt", reason="fork-only process race regression")
def test_separate_processes_atomically_claim_one_bot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    release = context.Event()
    processes = [
        context.Process(
            target=_competing_start_process,
            args=(str(root), results, release),
        )
        for _index in range(2)
    ]

    for process in processes:
        process.start()
    outcomes = [results.get(timeout=10)[0] for _index in range(2)]
    release.set()
    for process in processes:
        process.join(timeout=10)

    assert sorted(outcomes) == ["blocked", "started"]
    assert all(process.exitcode == 0 for process in processes)
    assert len(_evaluation_directories(root)) == 1


def test_lifecycle_status_and_failed_outcome_are_independent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    _persist_evaluation(
        root,
        evaluation_id="eval-completed-failed",
        lifecycle_status="completed",
        trials=[
            {
                "trial_id": "trial-1",
                "case_ref": "ifeval:ifeval-json-format",
                "target_id": "native",
                "target_fingerprint": "native-v1",
                "outcome": "failed",
            }
        ],
    )

    detail = EvaluationManager(root).get("eval-completed-failed")

    assert detail["status"] == "completed"
    assert detail["result"]["trials"][0]["outcome"] == "failed"
    assert detail["progress"]["completed"] == 1


def test_coverage_is_partitioned_by_target_fingerprint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    _persist_evaluation(
        root,
        evaluation_id="eval-fingerprints",
        trials=[
            {
                "trial_id": "trial-codex",
                "attempt": 1,
                "case_ref": "ifeval:ifeval-json-format",
                "suite_id": "ifeval",
                "case_id": "ifeval-json-format",
                "target_id": "codex",
                "target_fingerprint": "codex-model-a-high",
                "outcome": "passed",
                "score": 1.0,
                "finished_at": "2026-07-26T00:01:00+00:00",
            },
            {
                "trial_id": "trial-native",
                "attempt": 1,
                "case_ref": "ifeval:ifeval-json-format",
                "suite_id": "ifeval",
                "case_id": "ifeval-json-format",
                "target_id": "native",
                "target_fingerprint": "native-model-a-high",
                "outcome": "failed",
                "score": 0.0,
                "finished_at": "2026-07-26T00:01:01+00:00",
            },
        ],
    )

    coverage = EvaluationManager(root).coverage(
        bot_id="lingye-copilot-qq"
    )

    assert coverage["summary"] == {
        "case_count": 1,
        "failed_case_count": 1,
        "bot_count": 1,
        "target_count": 2,
    }
    assert {
        item["target_fingerprint"]
        for item in coverage["records"]
    } == {
        "codex-model-a-high",
        "native-model-a-high",
    }
    by_target = {
        item["target_id"]: item
        for item in coverage["records"]
    }
    assert by_target["codex"]["last_outcome"] == "passed"
    assert by_target["native"]["last_outcome"] == "failed"
    latest_native = by_target["native"]["history"][0]
    assert latest_native["trial_id"] == "trial-native"
    assert latest_native["attempt"] == 1
    assert latest_native["outcome"] == "failed"


def test_unified_api_lists_records_and_old_resources_are_gone(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    _persist_evaluation(
        root,
        evaluation_id="eval-api",
        lifecycle_status="completed",
    )
    manager = EvaluationManager(root)
    with _use_manager(manager):
        client = TestClient(app)
        profiles = client.get("/api/evals/profiles")
        records = client.get(
            "/api/evals/evaluations?status=completed"
        )
        detail = client.get("/api/evals/evaluations/eval-api")
        runs = client.get("/api/evals/runs")
        experiments = client.get("/api/evals/experiments")

    assert profiles.status_code == 200
    assert profiles.json()[0]["profile_id"] == "agent-comparison-mvp"
    assert records.status_code == 200
    assert records.json()[0]["evaluation_id"] == "eval-api"
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["result"]["trials"] == []
    assert runs.status_code == 404
    assert experiments.status_code == 404


def test_case_detail_routes_preserve_slashes_in_external_case_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-slash-case"
    case_id = "folder/external-case"
    case_ref = f"ifeval:{case_id}"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
        trials=[
            {
                "trial_id": "trial-slash",
                "case_ref": case_ref,
                "case_id": case_id,
                "target_id": "native",
                "target_fingerprint": "native-v1",
                "outcome": "passed",
            }
        ],
    )
    manager = EvaluationManager(root)
    encoded_case_id = quote(case_id, safe="")
    encoded_case_ref = quote(case_ref, safe="")

    with (
        patch(
            "console.backend.routes.evals.eval_control.get_case_descriptor",
            return_value={"case_id": case_id},
        ) as descriptor,
        _use_manager(manager),
    ):
        client = TestClient(app)
        catalog_case = client.get(
            f"/api/evals/suites/ifeval/cases/{encoded_case_id}"
        )
        result_case = client.get(
            f"/api/evals/evaluations/{evaluation_id}/cases/{encoded_case_ref}"
        )

    assert catalog_case.status_code == 200
    assert catalog_case.json()["case_id"] == case_id
    descriptor.assert_called_once()
    assert descriptor.call_args.args[:2] == ("ifeval", case_id)
    assert result_case.status_code == 200
    assert result_case.json()["case_ref"] == case_ref
    assert result_case.json()["trials"][0]["case_id"] == case_id


def test_evaluation_ids_reject_path_traversal(
    tmp_path: Path,
) -> None:
    manager = EvaluationManager(
        tmp_path / "evaluations",
        validator=_ready_validator(),
    )

    for invalid_id in ("../escape", "bad.id", "a" * 129):
        with pytest.raises(ValueError, match="invalid evaluation_id"):
            manager.get(invalid_id)

    with _use_manager(manager):
        response = TestClient(
            app,
            raise_server_exceptions=False,
        ).get("/api/evals/evaluations/bad!")

    assert response.status_code == 400


def test_evaluation_directory_symlink_alias_is_never_followed_or_deleted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    target_id = "eval-symlink-target"
    alias_id = "eval-symlink-alias"
    _persist_evaluation(
        root,
        evaluation_id=target_id,
        lifecycle_status="completed",
    )
    alias = root / alias_id
    alias.symlink_to(root / target_id, target_is_directory=True)
    manager = EvaluationManager(root, validator=_ready_validator())

    actions = (
        lambda: manager.get(alias_id),
        lambda: manager.case_detail(alias_id, "case"),
        lambda: manager.clone(alias_id, instance=_instance()),
        lambda: manager.cancel(alias_id),
        lambda: manager.delete(alias_id),
        lambda: manager.report_path(alias_id, "json"),
        lambda: next(manager.follow(alias_id)),
    )
    for action in actions:
        with pytest.raises(ValueError, match="cannot be a symlink"):
            action()

    assert alias.is_symlink()
    assert (root / target_id / "request.json").is_file()
    assert manager.get(target_id)["evaluation_id"] == target_id


@pytest.mark.parametrize("record_name", ["request.json", "state.json"])
def test_delete_rejects_record_id_mismatch(
    tmp_path: Path,
    record_name: str,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-delete-id-mismatch"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    path = root / evaluation_id / record_name
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evaluation_id"] = "eval-different-record"
    _write_json(path, payload)
    manager = EvaluationManager(root, validator=_ready_validator())

    actions = (
        lambda: manager.get(evaluation_id),
        lambda: manager.case_detail(evaluation_id, "case"),
        lambda: manager.clone(evaluation_id, instance=_instance()),
        lambda: manager.cancel(evaluation_id),
        lambda: manager.delete(evaluation_id),
        lambda: manager.report_path(evaluation_id, "json"),
        lambda: next(manager.follow(evaluation_id)),
    )
    for action in actions:
        with pytest.raises(ValueError, match="does not match"):
            action()

    assert (root / evaluation_id).is_dir()


@pytest.mark.parametrize("artifact_name", ["request.json", "state.json"])
def test_identity_artifact_symlink_is_rejected(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-identity-artifact-link"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    manager = EvaluationManager(root, validator=_ready_validator())
    artifact = root / evaluation_id / artifact_name
    external = tmp_path / f"external-{artifact_name}"
    external.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(external)

    with pytest.raises(ValueError, match="artifact cannot be a symlink"):
        manager.get(evaluation_id)

    assert external.is_file()


def test_result_artifact_symlink_is_rejected_before_read_or_export(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-result-artifact-link"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
        trials=[
            {
                "trial_id": "trial-1",
                "case_ref": "ifeval:ifeval-json-format",
                "target_id": "native",
                "target_fingerprint": "native-v1",
                "outcome": "passed",
            }
        ],
    )
    manager = EvaluationManager(root, validator=_ready_validator())
    result_path = root / evaluation_id / "result.json"
    external = tmp_path / "external-result.json"
    external.write_bytes(result_path.read_bytes())
    result_path.unlink()
    result_path.symlink_to(external)

    actions = (
        lambda: manager.get(evaluation_id),
        lambda: manager.case_detail(
            evaluation_id,
            "ifeval:ifeval-json-format",
        ),
        lambda: manager.report_path(evaluation_id, "json"),
        lambda: next(manager.follow(evaluation_id)),
    )
    for action in actions:
        with pytest.raises(ValueError, match="artifact cannot be a symlink"):
            action()

    assert external.is_file()


def test_summary_and_progress_artifact_symlinks_are_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-stream-artifact-links"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    directory = root / evaluation_id
    external_summary = tmp_path / "external-summary.md"
    external_summary.write_text("# Outside\n", encoding="utf-8")
    (directory / "summary.md").symlink_to(external_summary)
    external_progress = tmp_path / "external-progress.jsonl"
    external_progress.write_text('{"event":"outside"}\n', encoding="utf-8")
    (directory / "progress.jsonl").symlink_to(external_progress)
    manager = EvaluationManager(root, validator=_ready_validator())

    with pytest.raises(ValueError, match="artifact cannot be a symlink"):
        manager.report_path(evaluation_id, "markdown")
    with pytest.raises(ValueError, match="artifact cannot be a symlink"):
        next(manager.follow(evaluation_id))


def test_result_id_mismatch_is_rejected_before_read_export_or_stream(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-result-id-mismatch"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    result_path = root / evaluation_id / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["evaluation_id"] = "eval-other-result"
    _write_json(result_path, result)
    manager = EvaluationManager(root, validator=_ready_validator())

    actions = (
        lambda: manager.get(evaluation_id),
        lambda: manager.case_detail(evaluation_id, "case"),
        lambda: manager.report_path(evaluation_id, "json"),
        lambda: next(manager.follow(evaluation_id)),
    )
    for action in actions:
        with pytest.raises(ValueError, match="result record"):
            action()


def test_activity_claim_symlink_is_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    manager = EvaluationManager(root, validator=_ready_validator())
    bot_id = _instance().instance_id
    external = tmp_path / "external-claim.json"
    external.write_text(
        json.dumps(
            {
                "bot_id": bot_id,
                "evaluation_id": "eval-external-claim",
                "owner_pid": os.getpid(),
                "worker_pid": None,
            }
        ),
        encoding="utf-8",
    )
    manager._claim_path(bot_id).symlink_to(external)

    with pytest.raises(RuntimeError, match="claim cannot be a symlink"):
        manager.active_for_bot(bot_id)
    with pytest.raises(RuntimeError, match="claim cannot be a symlink"):
        manager.start(
            instance=_instance(),
            request={
                "kind": "comparison",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )

    assert external.is_file()


def test_worker_identity_requires_exact_output_argument(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "eval-a"
    adjacent = tmp_path / "eval-ab"
    command = [
        "python",
        "-m",
        "chatcopilot",
        "evals",
        "run",
        "--output",
        str(expected),
    ]

    assert EvaluationManager._argv_matches_evaluation(command, expected)
    assert not EvaluationManager._argv_matches_evaluation(command, adjacent)
    command[-1] = str(adjacent)
    assert not EvaluationManager._argv_matches_evaluation(command, expected)
    command[-2:] = [f"--output={expected}"]
    assert EvaluationManager._argv_matches_evaluation(command, expected)
    command.extend(["--output", str(adjacent)])
    assert not EvaluationManager._argv_matches_evaluation(command, expected)


def test_windows_command_line_parser_preserves_quoted_output_token() -> None:
    argv = [
        r"C:\Program Files\Python\python.exe",
        "-m",
        "chatcopilot",
        "evals",
        "run",
        "--output",
        r"C:\Evaluation Reports\eval-a",
    ]

    command_line = subprocess.list2cmdline(argv)

    assert EvaluationManager._split_windows_command_line(command_line) == argv


def test_manager_recovers_only_complete_checkpoint_as_partial(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    _persist_evaluation(
        root,
        evaluation_id="eval-active-checkpoint",
        lifecycle_status="running",
        trials=[
            {
                "trial_id": "trial-1",
                "case_ref": "ifeval:ifeval-json-format",
                "case_id": "ifeval-json-format",
                "target_id": "native",
                "target_fingerprint": "native-v1",
                "outcome": "passed",
            }
        ],
    )

    detail = EvaluationManager(root).get("eval-active-checkpoint")

    assert detail["status"] == "partial"
    assert detail["result"]["status"] == "partial"
    assert len(detail["result"]["trials"]) == 1


def test_manager_recovers_empty_active_evaluation_as_interrupted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    _persist_evaluation(
        root,
        evaluation_id="eval-active-empty",
        lifecycle_status="queued",
    )
    state_path = root / "eval-active-empty" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pid"] = 4321
    _write_json(state_path, state)

    with (
        patch.object(
            EvaluationManager,
            "_pid_matches_evaluation",
            return_value=False,
        ),
        patch.object(
            EvaluationManager,
            "_pid_exists",
            return_value=False,
        ),
        patch.object(EvaluationManager, "_terminate_pid") as terminate,
    ):
        detail = EvaluationManager(root).get("eval-active-empty")

    terminate.assert_not_called()
    assert detail["status"] == "interrupted"
    assert detail["progress"]["completed"] == 0


def test_manager_preserves_active_record_with_live_worker_pid(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    _persist_evaluation(
        root,
        evaluation_id="eval-live-worker",
        lifecycle_status="running",
    )
    state_path = root / "eval-live-worker" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pid"] = 4321
    _write_json(state_path, state)

    with patch.object(
        EvaluationManager,
        "_pid_matches_evaluation",
        return_value=True,
    ):
        detail = EvaluationManager(root).get("eval-live-worker")

    assert detail["status"] == "running"


def test_stopped_worker_restores_terminal_result_without_overwriting_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-restart-completed"
    bootstrap = EvaluationManager(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
        trials=[
            {
                "trial_id": "trial-1",
                "case_ref": "ifeval:ifeval-json-format",
                "target_id": "native",
                "target_fingerprint": "native-v1",
                "outcome": "passed",
            }
        ],
    )
    state_path = root / evaluation_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"status": "running", "pid": 4321, "finished_at": None})
    _write_json(state_path, state)
    with bootstrap._creation_guard(), bootstrap._lock:
        bootstrap._create_claim(_instance().instance_id, evaluation_id)
        bootstrap._update_claim(
            _instance().instance_id,
            evaluation_id,
            worker_pid=4321,
        )

    worker_alive = True

    def matches_worker(_pid: int, _directory: Path) -> bool:
        return worker_alive

    with patch.object(
        EvaluationManager,
        "_pid_matches_evaluation",
        side_effect=matches_worker,
    ), patch.object(
        EvaluationManager,
        "_pid_exists",
        side_effect=lambda _pid: worker_alive,
    ):
        observer = EvaluationManager(root, validator=_ready_validator())
        assert observer.get(evaluation_id)["status"] == "running"
        worker_alive = False
        assert observer.active_for_bot(_instance().instance_id) is None

    detail = observer.get(evaluation_id)
    assert detail["status"] == "completed"
    assert detail["result"]["status"] == "completed"
    assert detail["result"]["trials"][0]["outcome"] == "passed"
    assert not list(root.glob(".active-*.json"))


def test_new_manager_cancels_only_verified_inherited_worker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-inherited-cancel"
    bootstrap = EvaluationManager(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="running",
    )
    state_path = root / evaluation_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pid"] = None
    _write_json(state_path, state)
    with bootstrap._creation_guard(), bootstrap._lock:
        bootstrap._create_claim(_instance().instance_id, evaluation_id)
        bootstrap._update_claim(
            _instance().instance_id,
            evaluation_id,
            worker_pid=4321,
        )

    worker_alive = True

    def matches_worker(_pid: int, _directory: Path) -> bool:
        return worker_alive

    def terminate_worker(pid: int) -> None:
        nonlocal worker_alive
        assert pid == 4321
        worker_alive = False

    with (
        patch.object(
            EvaluationManager,
            "_pid_matches_evaluation",
            side_effect=matches_worker,
        ),
        patch.object(
            EvaluationManager,
            "_pid_exists",
            side_effect=lambda _pid: worker_alive,
        ),
        patch.object(
            EvaluationManager,
            "_terminate_pid",
            side_effect=terminate_worker,
        ) as terminate,
    ):
        observer = EvaluationManager(root, validator=_ready_validator())
        cancelled = observer.cancel(evaluation_id)

    terminate.assert_called_once_with(4321)
    assert cancelled["status"] == "cancelled"
    assert not list(root.glob(".active-*.json"))


@pytest.mark.parametrize("pid_source", ["state", "claim"])
def test_cancel_fails_closed_for_unverified_live_inherited_pid(
    tmp_path: Path,
    pid_source: str,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-unverified-cancel"
    bootstrap = EvaluationManager(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="running",
    )
    state_path = root / evaluation_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pid"] = 4321 if pid_source == "state" else None
    _write_json(state_path, state)
    if pid_source == "claim":
        with bootstrap._creation_guard(), bootstrap._lock:
            bootstrap._create_claim(_instance().instance_id, evaluation_id)
            bootstrap._update_claim(
                _instance().instance_id,
                evaluation_id,
                worker_pid=4321,
            )

    with patch.object(
        EvaluationManager,
        "_pid_matches_evaluation",
        return_value=True,
    ):
        observer = EvaluationManager(root, validator=_ready_validator())
    with (
        patch.object(
            EvaluationManager,
            "_pid_matches_evaluation",
            return_value=False,
        ),
        patch.object(
            EvaluationManager,
            "_pid_exists",
            return_value=True,
        ),
        patch.object(EvaluationManager, "_terminate_pid") as terminate,
    ):
        with pytest.raises(RuntimeError, match="identity cannot be verified"):
            observer.cancel(evaluation_id)
        with pytest.raises(RuntimeError, match="active evaluation"):
            observer.start(
                instance=_instance(),
                request={
                    "kind": "comparison",
                    "profile_id": "agent-comparison-mvp",
                    "preset": "quick",
                },
            )

    terminate.assert_not_called()
    assert observer.get(evaluation_id)["status"] == "running"
    assert evaluation_id not in observer._cancelled
    if pid_source == "claim":
        assert list(root.glob(".active-*.json"))


def test_cancel_releases_stale_pid_only_after_nonexistence_is_proven(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-exited-cancel"
    EvaluationManager(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="running",
    )
    state_path = root / evaluation_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pid"] = 4321
    _write_json(state_path, state)

    with patch.object(
        EvaluationManager,
        "_pid_matches_evaluation",
        return_value=True,
    ):
        observer = EvaluationManager(root, validator=_ready_validator())
    with (
        patch.object(
            EvaluationManager,
            "_pid_matches_evaluation",
            return_value=False,
        ),
        patch.object(
            EvaluationManager,
            "_pid_exists",
            return_value=False,
        ),
        patch.object(EvaluationManager, "_terminate_pid") as terminate,
    ):
        cancelled = observer.cancel(evaluation_id)

    terminate.assert_not_called()
    assert cancelled["status"] == "cancelled"


def test_rerun_quick_revalidates_without_resolved_overrides(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    _persist_evaluation(
        root,
        evaluation_id="eval-rerun-source",
        lifecycle_status="completed",
    )
    captured: list[dict[str, Any]] = []
    manager = EvaluationManager(
        root,
        validator=_ready_validator(captured),
    )
    with (
        patch.object(manager, "_spawn"),
        _use_manager(manager),
    ):
        response = TestClient(app).post(
            "/api/evals/evaluations/eval-rerun-source/rerun"
        )

    assert response.status_code == 200
    assert set(captured[0]) == {
        "kind",
        "bot_id",
        "profile_id",
        "preset",
    }
    assert response.json()["evaluation_id"] != "eval-rerun-source"


def test_terminal_state_with_live_claim_blocks_delete_rerun_and_new_start(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-terminal-live-worker"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    owner = EvaluationManager(root, validator=_ready_validator())
    with owner._creation_guard(), owner._lock:
        owner._create_claim(_instance().instance_id, evaluation_id)
        owner._update_claim(
            _instance().instance_id,
            evaluation_id,
            worker_pid=4321,
        )

    with patch.object(
        EvaluationManager,
        "_pid_matches_evaluation",
        return_value=True,
    ):
        observer = EvaluationManager(root, validator=_ready_validator())
        with pytest.raises(RuntimeError, match="cannot be deleted"):
            observer.delete(evaluation_id)
        with pytest.raises(RuntimeError, match="active evaluation"):
            observer.clone(evaluation_id, instance=_instance())
        with pytest.raises(RuntimeError, match="active evaluation"):
            observer.start(
                instance=_instance(),
                request={
                    "kind": "comparison",
                    "profile_id": "agent-comparison-mvp",
                    "preset": "quick",
                },
            )

    assert (root / evaluation_id).is_dir()


def test_delete_waits_for_managed_process_and_monitor_tolerates_removal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-delete-monitor-race"
    bot_id = _instance().instance_id
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    manager = EvaluationManager(root, validator=_ready_validator())
    process = Mock()
    process.pid = 4321
    process.stdout = iter(())
    process.poll.return_value = None
    process.wait.return_value = 0
    manager._processes[evaluation_id] = process
    manager._process_bot_ids[evaluation_id] = bot_id
    with manager._creation_guard(), manager._lock:
        manager._create_claim(bot_id, evaluation_id)
        manager._update_claim(
            bot_id,
            evaluation_id,
            worker_pid=process.pid,
        )

    with pytest.raises(RuntimeError, match="cannot be deleted"):
        manager.delete(evaluation_id)

    process.poll.return_value = 0
    manager.delete(evaluation_id)
    manager._monitor(evaluation_id, process, (), bot_id)

    assert not (root / evaluation_id).exists()
    assert evaluation_id not in manager._processes


def test_monitor_writes_only_run_log_not_structured_progress(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-single-progress-writer"
    bot_id = _instance().instance_id
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    manager = EvaluationManager(root, validator=_ready_validator())
    process = Mock()
    process.stdout = iter(
        ['__EVAL_EVENT__ {"event":"trial_completed"}\n', "plain output\n"]
    )
    process.wait.return_value = 0
    manager._processes[evaluation_id] = process
    manager._process_bot_ids[evaluation_id] = bot_id

    manager._monitor(evaluation_id, process, (), bot_id)

    directory = root / evaluation_id
    assert (directory / "run.log").read_text(encoding="utf-8") == (
        '__EVAL_EVENT__ {"event":"trial_completed"}\nplain output\n'
    )
    assert not (directory / "progress.jsonl").exists()


def test_close_tolerates_externally_removed_evaluation_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-close-removed"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    manager = EvaluationManager(root, validator=_ready_validator())
    process = Mock()
    process.poll.return_value = 0
    manager._processes[evaluation_id] = process
    manager._process_bot_ids[evaluation_id] = _instance().instance_id
    shutil.rmtree(root / evaluation_id)

    manager.close()

    assert not (root / evaluation_id).exists()
    assert evaluation_id not in manager._processes


def test_case_stream_export_and_delete_share_one_evaluation_resource(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-resource-api"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
        trials=[
            {
                "trial_id": "trial-1",
                "case_ref": "ifeval:ifeval-json-format",
                "case_id": "ifeval-json-format",
                "target_id": "native",
                "target_fingerprint": "native-v1",
                "outcome": "passed",
            }
        ],
    )
    directory = root / evaluation_id
    (directory / "summary.md").write_text(
        "# Evaluation\n",
        encoding="utf-8",
    )
    (directory / "progress.jsonl").write_text(
        json.dumps(
            {
                "event": "trial_completed",
                "case_ref": "ifeval:ifeval-json-format",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manager = EvaluationManager(root)
    with _use_manager(manager):
        client = TestClient(app)
        case = client.get(
            f"/api/evals/evaluations/{evaluation_id}/cases/"
            "ifeval:ifeval-json-format"
        )
        stream = client.get(
            f"/api/evals/evaluations/{evaluation_id}/stream"
        )
        export = client.get(
            f"/api/evals/evaluations/{evaluation_id}/export/markdown"
        )
        removed = client.delete(
            f"/api/evals/evaluations/{evaluation_id}"
        )

    assert case.status_code == 200
    assert case.json()["trials"][0]["outcome"] == "passed"
    assert stream.status_code == 200
    assert '"event": "trial_completed"' in stream.text
    assert '"status": "completed"' in stream.text
    assert export.status_code == 200
    assert export.text == "# Evaluation\n"
    assert removed.status_code == 200
    assert not directory.exists()
