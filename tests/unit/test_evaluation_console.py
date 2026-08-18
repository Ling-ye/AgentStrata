from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from chatcopilot.evals import managed_worker as managed_worker_module
from chatcopilot.evals.application import (
    EvaluationApplication,
    EvaluationBlocked,
    EvaluationBotRef as BotInstance,
)
from chatcopilot.evals.application import catalog as evaluation_catalog
from chatcopilot.evals.service import (
    EvaluationReport,
    EvaluationReportStream,
    EvaluationServiceError,
)
from console.backend.app import app
from console.control.discovery import repo_root


def _instance() -> BotInstance:
    return BotInstance(
        instance_id="lingye-copilot-qq",
        bot_spec=repo_root() / "bots/lingye-copilot-qq/bot.yaml",
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


class _InProcessEvaluationClient:
    def __init__(self, manager: EvaluationApplication) -> None:
        self.manager = manager

    def health(self) -> dict[str, Any]:
        return {"ready": True, "service": "in-process-test"}

    def list_profiles(self) -> list[dict[str, Any]]:
        return evaluation_catalog.list_profile_descriptors()

    def list_suites(self, *, bot_id: str | None = None) -> list[dict[str, Any]]:
        del bot_id
        return evaluation_catalog.list_suite_descriptors()

    def list_cases(
        self,
        suite_id: str,
        *,
        bot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del bot_id
        return evaluation_catalog.list_case_summaries(suite_id)

    def get_case(
        self,
        suite_id: str,
        case_id: str,
        *,
        bot_id: str | None = None,
    ) -> dict[str, Any]:
        del bot_id
        return self._call(
            evaluation_catalog.get_case_descriptor,
            suite_id,
            case_id,
        )

    def list(self, **filters: Any) -> list[dict[str, Any]]:
        return self._call(self.manager.list, **filters)

    def get(
        self,
        evaluation_id: str,
        *,
        include_result: bool = True,
    ) -> dict[str, Any]:
        return self._call(
            self.manager.get,
            evaluation_id,
            include_result=include_result,
        )

    def case_detail(self, evaluation_id: str, case_ref: str) -> dict[str, Any]:
        return self._call(self.manager.case_detail, evaluation_id, case_ref)

    def coverage(self, *, bot_id: str | None = None) -> dict[str, Any]:
        return self._call(self.manager.coverage, bot_id=bot_id)

    def follow(self, evaluation_id: str):
        try:
            yield from self.manager.follow(evaluation_id)
        except (KeyError, RuntimeError, ValueError, OSError) as exc:
            raise self._translate(exc) from exc

    def start(self, *, bot_id: str, request: Mapping[str, Any]):
        return self._call(self.manager.start, bot_id=bot_id, request=request)

    def clone(self, evaluation_id: str):
        return self._call(self.manager.clone, evaluation_id)

    def cancel(self, evaluation_id: str):
        return self._call(self.manager.cancel, evaluation_id)

    def delete(self, evaluation_id: str):
        return self._call(self.manager.delete, evaluation_id)

    def report(self, evaluation_id: str, kind: str) -> EvaluationReport:
        path = self._call(self.manager.report_path, evaluation_id, kind)
        return EvaluationReport(
            filename=path.name,
            media_type=("application/json" if kind == "json" else "text/markdown"),
            content=path.read_bytes(),
        )

    def report_stream(self, evaluation_id: str, kind: str) -> EvaluationReportStream:
        report = self.report(evaluation_id, kind)
        return EvaluationReportStream(
            filename=report.filename,
            media_type=report.media_type,
            chunks=iter((report.content,)),
        )

    @staticmethod
    def _call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except EvaluationBlocked as exc:
            raise EvaluationServiceError(
                "evaluation_blocked",
                str(exc.payload.get("message") or exc),
                checks=list(exc.payload.get("checks") or ()),
            ) from exc
        except (KeyError, RuntimeError, ValueError, OSError) as exc:
            raise _InProcessEvaluationClient._translate(exc) from exc

    @staticmethod
    def _translate(exc: Exception) -> EvaluationServiceError:
        if isinstance(exc, KeyError):
            return EvaluationServiceError("not_found", "not found")
        if isinstance(exc, RuntimeError):
            return EvaluationServiceError("conflict", str(exc))
        return EvaluationServiceError("invalid_request", str(exc))


@contextmanager
def _use_manager(manager: EvaluationApplication) -> Iterator[None]:
    previous = app.state.evaluations
    app.state.evaluations = _InProcessEvaluationClient(manager)
    try:
        yield
    finally:
        app.state.evaluations = previous


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)


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
    manager = EvaluationApplication(
        Path(root),
        validator=_ready_validator(),
    )
    try:
        with patch.object(manager, "_spawn"):
            evaluation = manager.start(
                bot_id=_instance().instance_id,
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
    manager = EvaluationApplication(
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
    manager = EvaluationApplication(
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

    manager = EvaluationApplication(
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
            bot_id=_instance().instance_id,
            request={
                "kind": "comparison",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )


@pytest.mark.parametrize("raw_confirmation", ["false", "true", "yes", 1])
def test_application_rejects_non_boolean_external_write_confirmation_before_creation(
    tmp_path: Path,
    raw_confirmation: object,
) -> None:
    manager = EvaluationApplication(tmp_path / "evaluations")

    with patch.object(manager, "_spawn") as spawn, pytest.raises(EvaluationBlocked) as caught:
        manager.start(
            bot_id=_instance().instance_id,
            request={
                "kind": "suite",
                "suite_id": "agentstrata-capabilities-v1",
                "preset": "full",
                "case_ids": [],
                "repetitions": 1,
                "max_wall_seconds": 0,
                "seed": 0,
                "options": {},
                "confirm_external_write": raw_confirmation,
                "dry_run": False,
                "llm_judge": False,
            },
        )

    assert caught.value.payload["code"] == "preflight_failed"
    failed_checks = [item for item in caught.value.payload["checks"] if not item.get("ok")]
    assert any(
        item.get("code") == "request"
        and "confirm_external_write must be a boolean" in str(item.get("detail") or "")
        for item in failed_checks
    )
    assert not _evaluation_directories(manager.root)
    assert not list(manager.root.glob(".active-*.json"))
    spawn.assert_not_called()


def test_startup_failure_is_sanitized_before_any_error_is_persisted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    manager = EvaluationApplication(root, validator=_ready_validator())
    secret = "startup-secret-value"
    absolute_path = repo_root() / "private" / "credentials.json"
    raw_error = f"token {secret} failed at {absolute_path}"

    with (
        patch(
            "chatcopilot.evals.application.controller.bot_env",
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
            bot_id=_instance().instance_id,
            request={
                "kind": "comparison",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )

    directories = _evaluation_directories(root)
    assert len(directories) == 1
    state = json.loads((directories[0] / "state.json").read_text(encoding="utf-8"))
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


def test_start_reuses_one_machine_first_env_snapshot_for_preflight_and_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "CHATCOPILOT_EVAL_SNAPSHOT_TEST"
    monkeypatch.setenv(key, "service-value")
    observed: list[str] = []

    def validator(_bot: BotInstance, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        observed.append(os.environ[key])
        return _ready_validator()(_bot, _request)

    manager = EvaluationApplication(tmp_path / "evaluations", validator=validator)

    def capture_spawn(evaluation_id: str, _bot: BotInstance) -> None:
        observed.append(manager._spawn_env_snapshots[evaluation_id][key])

    with (
        patch(
            "chatcopilot.evals.application.controller.bot_env",
            return_value={key: "captured-value"},
        ) as load_env,
        patch.object(manager, "_spawn", side_effect=capture_spawn),
    ):
        manager.start(
            bot_id=_instance().instance_id,
            request={
                "kind": "comparison",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )

    assert observed == ["service-value", "service-value"]
    assert os.environ[key] == "service-value"
    load_env.assert_called_once()
    assert manager._spawn_env_snapshots == {}


def test_same_bot_allows_only_one_active_evaluation(
    tmp_path: Path,
) -> None:
    manager = EvaluationApplication(
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
    first = EvaluationApplication(root, validator=_ready_validator())
    second = EvaluationApplication(root, validator=_ready_validator())

    def start(manager: EvaluationApplication) -> str:
        with patch.object(manager, "_spawn"):
            try:
                manager.start(
                    bot_id=_instance().instance_id,
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

    detail = EvaluationApplication(root).get("eval-completed-failed")

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

    coverage = EvaluationApplication(root).coverage(bot_id="lingye-copilot-qq")

    assert coverage["summary"] == {
        "case_count": 1,
        "failed_case_count": 1,
        "bot_count": 1,
        "target_count": 2,
    }
    assert {item["target_fingerprint"] for item in coverage["records"]} == {
        "codex-model-a-high",
        "native-model-a-high",
    }
    by_target = {item["target_id"]: item for item in coverage["records"]}
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
    manager = EvaluationApplication(root)
    with _use_manager(manager):
        client = TestClient(app)
        profiles = client.get("/api/evals/profiles")
        records = client.get("/api/evals/evaluations?status=completed")
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
    manager = EvaluationApplication(root)
    encoded_case_id = quote(case_id, safe="")
    encoded_case_ref = quote(case_ref, safe="")

    with (
        patch(
            "chatcopilot.evals.application.catalog.get_case_descriptor",
            return_value={"case_id": case_id},
        ) as descriptor,
        _use_manager(manager),
    ):
        client = TestClient(app)
        catalog_case = client.get(f"/api/evals/suites/ifeval/cases/{encoded_case_id}")
        result_case = client.get(f"/api/evals/evaluations/{evaluation_id}/cases/{encoded_case_ref}")

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
    manager = EvaluationApplication(
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
    manager = EvaluationApplication(root, validator=_ready_validator())

    actions = (
        lambda: manager.get(alias_id),
        lambda: manager.case_detail(alias_id, "case"),
        lambda: manager.clone(alias_id),
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
    manager = EvaluationApplication(root, validator=_ready_validator())

    actions = (
        lambda: manager.get(evaluation_id),
        lambda: manager.case_detail(evaluation_id, "case"),
        lambda: manager.clone(evaluation_id),
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
    manager = EvaluationApplication(root, validator=_ready_validator())
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
    manager = EvaluationApplication(root, validator=_ready_validator())
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
    manager = EvaluationApplication(root, validator=_ready_validator())

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
    manager = EvaluationApplication(root, validator=_ready_validator())

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
    manager = EvaluationApplication(root, validator=_ready_validator())
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
            bot_id=_instance().instance_id,
            request={
                "kind": "comparison",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )

    assert external.is_file()


def test_evaluation_root_rejects_symlink_ancestor(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    linked_parent = tmp_path / "reports"
    linked_parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="cannot contain a symlink"):
        EvaluationApplication(linked_parent / "evaluations")

    assert not (external / "evaluations").exists()


def test_existing_evaluation_directory_and_artifacts_require_private_modes(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX ownership and mode boundary")
    root = tmp_path / "evaluations"
    evaluation_id = "eval-private-modes"
    _persist_evaluation(root, evaluation_id=evaluation_id)
    directory = root / evaluation_id
    result_path = directory / "result.json"

    result_path.chmod(0o644)
    application = EvaluationApplication(root)
    with pytest.raises(PermissionError, match="mode 0600"):
        application.get(evaluation_id)

    result_path.chmod(0o600)
    directory.chmod(0o755)
    with pytest.raises(PermissionError, match="mode 0700"):
        EvaluationApplication(root)


def test_existing_evaluation_artifact_hardlink_is_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX hard-link boundary")
    root = tmp_path / "evaluations"
    evaluation_id = "eval-hardlink"
    _persist_evaluation(root, evaluation_id=evaluation_id)
    result_path = root / evaluation_id / "result.json"
    alias = tmp_path / "result-alias.json"
    os.link(result_path, alias)

    application = EvaluationApplication(root)
    with pytest.raises(ValueError, match="exactly one hard link"):
        application.get(evaluation_id)


def test_existing_activity_claim_requires_private_mode(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX ownership and mode boundary")
    application = EvaluationApplication(
        tmp_path / "evaluations",
        validator=_ready_validator(),
    )
    bot_id = _instance().instance_id
    application._create_claim(bot_id, "eval-private-claim")
    claim_path = application._claim_path(bot_id)
    claim_path.chmod(0o644)

    with pytest.raises(RuntimeError, match="otherwise unsafe"):
        application.active_for_bot(bot_id)


def test_worker_identity_requires_exact_output_argument(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "eval-a"
    adjacent = tmp_path / "eval-ab"
    command = [
        "python",
        "-m",
        "chatcopilot.evals.managed_worker",
        "--output",
        str(expected),
    ]

    assert EvaluationApplication._argv_matches_evaluation(command, expected)
    assert not EvaluationApplication._argv_matches_evaluation(command, adjacent)
    command[-1] = str(adjacent)
    assert not EvaluationApplication._argv_matches_evaluation(command, expected)
    command[-2:] = [f"--output={expected}"]
    assert EvaluationApplication._argv_matches_evaluation(command, expected)
    command.extend(["--output", str(adjacent)])
    assert not EvaluationApplication._argv_matches_evaluation(command, expected)
    public_cli = [
        "python",
        "-m",
        "chatcopilot",
        "evals",
        "run",
        "--output",
        str(expected),
    ]
    assert not EvaluationApplication._argv_matches_evaluation(public_cli, expected)


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

    assert EvaluationApplication._split_windows_command_line(command_line) == argv


def test_application_recovers_checkpoint_without_rewriting_core_result(
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

    result_path = root / "eval-active-checkpoint" / "result.json"
    original_result = result_path.read_bytes()

    detail = EvaluationApplication(root).get("eval-active-checkpoint")

    assert detail["status"] == "partial"
    assert detail["result"]["status"] == "running"
    assert len(detail["result"]["trials"]) == 1
    assert result_path.read_bytes() == original_result


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
            EvaluationApplication,
            "_pid_matches_evaluation",
            return_value=False,
        ),
        patch.object(
            EvaluationApplication,
            "_pid_exists",
            return_value=False,
        ),
        patch.object(EvaluationApplication, "_request_pid_stop") as request_stop,
        patch.object(EvaluationApplication, "_kill_pid") as kill,
    ):
        detail = EvaluationApplication(root).get("eval-active-empty")

    request_stop.assert_not_called()
    kill.assert_not_called()
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
        EvaluationApplication,
        "_pid_matches_evaluation",
        return_value=True,
    ):
        detail = EvaluationApplication(root).get("eval-live-worker")

    assert detail["status"] == "running"


def test_recovery_adopts_worker_discovered_after_pid_persistence_gap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-discovered-worker"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="running",
    )

    with (
        patch.object(
            EvaluationApplication,
            "_discover_worker_pids",
            return_value=[4321],
        ),
        patch.object(
            EvaluationApplication,
            "_worker_pid_status",
            return_value="matched",
        ),
        patch("chatcopilot.evals.application.controller.threading.Thread.start") as start_watch,
    ):
        EvaluationApplication(root, validator=_ready_validator())

    state = json.loads((root / evaluation_id / "state.json").read_text(encoding="utf-8"))
    claims = list(root.glob(".active-*.json"))
    assert state["status"] == "running"
    assert state["pid"] == 4321
    assert len(claims) == 1
    claim = json.loads(claims[0].read_text(encoding="utf-8"))
    assert claim["evaluation_id"] == evaluation_id
    assert claim["worker_pid"] == 4321
    start_watch.assert_called_once()


def test_recovery_does_not_rewrite_an_already_matching_live_claim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-matching-live-claim"
    bot_id = _instance().instance_id
    bootstrap = EvaluationApplication(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="running",
    )
    state_path = root / evaluation_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"status": "running", "pid": 4321, "started_at": "2026-08-17T00:00:00Z"})
    _write_json(state_path, state)
    with bootstrap._creation_guard(), bootstrap._lock:
        bootstrap._create_claim(bot_id, evaluation_id)
        bootstrap._update_claim(bot_id, evaluation_id, worker_pid=4321)
    claim_path = bootstrap._claim_path(bot_id)
    before = claim_path.lstat()
    before_bytes = claim_path.read_bytes()

    with (
        patch.object(EvaluationApplication, "_worker_pid_status", return_value="matched"),
        patch("chatcopilot.evals.application.controller.threading.Thread.start"),
    ):
        EvaluationApplication(root, validator=_ready_validator())

    after = claim_path.lstat()
    assert claim_path.read_bytes() == before_bytes
    assert (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


@pytest.mark.parametrize(
    ("claim_overrides", "state_overrides"),
    (
        ({"worker_pid": 1}, {}),
        ({"evaluation_id": "eval-foreign"}, {}),
        ({"bot_id": "foreign-bot"}, {}),
        ({"unexpected": True}, {}),
        ({}, {"status": "queued"}),
        ({}, {"pid": 1}),
        ({}, {"evaluation_id": "eval-foreign"}),
        ({}, {"kind": "comparison"}),
    ),
)
def test_managed_worker_requires_current_pid_in_state_and_claim(
    tmp_path: Path,
    claim_overrides: dict[str, Any],
    state_overrides: dict[str, Any],
) -> None:
    root = tmp_path / "evaluations"
    application = EvaluationApplication(root, validator=_ready_validator())
    evaluation_id = "eval-managed-startup-identity"
    bot_id = _instance().instance_id
    output = root / evaluation_id
    output.mkdir(mode=0o700)
    claim_path = application._claim_path(bot_id)
    _write_json(
        claim_path,
        {
            "bot_id": bot_id,
            "evaluation_id": evaluation_id,
            "owner_pid": os.getpid(),
            "worker_pid": os.getpid(),
            "created_at": "2026-08-17T00:00:00Z",
            **claim_overrides,
        },
    )
    _write_json(
        output / "state.json",
        {
            "evaluation_id": evaluation_id,
            "kind": "suite",
            "status": "running",
            "pid": os.getpid(),
            "started_at": "2026-08-17T00:00:00Z",
            **state_overrides,
        },
    )

    with pytest.raises(ValueError, match="claim|state"):
        managed_worker_module._managed_claim_path(
            output,
            {"bot_id": bot_id, "kind": "suite"},
        )


def test_managed_worker_accepts_matching_current_pid_state_and_claim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    application = EvaluationApplication(root, validator=_ready_validator())
    evaluation_id = "eval-managed-startup-identity"
    bot_id = _instance().instance_id
    output = root / evaluation_id
    output.mkdir(mode=0o700)
    claim_path = application._claim_path(bot_id)
    _write_json(
        claim_path,
        {
            "bot_id": bot_id,
            "evaluation_id": evaluation_id,
            "owner_pid": os.getpid(),
            "worker_pid": os.getpid(),
            "created_at": "2026-08-17T00:00:00Z",
        },
    )
    _write_json(
        output / "state.json",
        {
            "evaluation_id": evaluation_id,
            "kind": "suite",
            "status": "running",
            "pid": os.getpid(),
            "started_at": "2026-08-17T00:00:00Z",
        },
    )

    assert managed_worker_module._managed_claim_path(
        output,
        {"bot_id": bot_id, "kind": "suite"},
    ) == claim_path


def test_spawn_publish_failure_closes_gate_before_core_artifact_writes(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX inherited startup gate")
    root = tmp_path / "evaluations"
    application = EvaluationApplication(root, validator=_ready_validator())

    with (
        patch.object(
            application,
            "_update_claim",
            side_effect=OSError("injected claim persistence failure"),
        ),
        pytest.raises(RuntimeError, match="claim persistence failure"),
    ):
        application.start(
            bot_id=_instance().instance_id,
            request={
                "kind": "comparison",
                "profile_id": "agent-comparison-mvp",
                "preset": "quick",
            },
        )

    directories = _evaluation_directories(root)
    assert len(directories) == 1
    directory = directories[0]
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "error"
    assert not (directory / "run.log").exists()
    assert not (directory / "result.json").exists()
    assert not (directory / "progress.jsonl").exists()
    assert not list(root.glob(".active-*.json"))
    assert not application._processes


def test_stopped_worker_restores_terminal_result_without_overwriting_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-restart-completed"
    bootstrap = EvaluationApplication(root, validator=_ready_validator())
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

    with (
        patch.object(
            EvaluationApplication,
            "_pid_matches_evaluation",
            side_effect=matches_worker,
        ),
        patch.object(
            EvaluationApplication,
            "_pid_exists",
            side_effect=lambda _pid: worker_alive,
        ),
    ):
        observer = EvaluationApplication(root, validator=_ready_validator())
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
    bootstrap = EvaluationApplication(root, validator=_ready_validator())
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
            EvaluationApplication,
            "_pid_matches_evaluation",
            side_effect=matches_worker,
        ),
        patch.object(
            EvaluationApplication,
            "_pid_exists",
            side_effect=lambda _pid: worker_alive,
        ),
        patch.object(
            EvaluationApplication,
            "_request_pid_stop",
            side_effect=terminate_worker,
        ) as request_stop,
        patch.object(EvaluationApplication, "_kill_pid") as kill,
    ):
        observer = EvaluationApplication(root, validator=_ready_validator())
        cancelled = observer.cancel(evaluation_id)

    request_stop.assert_called_once_with(4321)
    kill.assert_not_called()
    assert cancelled["status"] == "cancelled"
    assert not list(root.glob(".active-*.json"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX cooperative signal boundary")
def test_cooperative_cancel_signals_only_the_managed_worker_pid() -> None:
    with (
        patch("chatcopilot.evals.application.controller.os.kill") as kill,
        patch("chatcopilot.evals.application.controller.os.killpg") as kill_group,
    ):
        EvaluationApplication._request_pid_stop(4321)

    kill.assert_called_once_with(4321, signal.SIGTERM)
    kill_group.assert_not_called()


@pytest.mark.parametrize("pid_source", ["state", "claim"])
def test_cancel_fails_closed_for_unverified_live_inherited_pid(
    tmp_path: Path,
    pid_source: str,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-unverified-cancel"
    bootstrap = EvaluationApplication(root, validator=_ready_validator())
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
        EvaluationApplication,
        "_pid_matches_evaluation",
        return_value=True,
    ):
        observer = EvaluationApplication(root, validator=_ready_validator())
    with (
        patch.object(
            EvaluationApplication,
            "_pid_matches_evaluation",
            return_value=False,
        ),
        patch.object(
            EvaluationApplication,
            "_pid_exists",
            return_value=True,
        ),
        patch.object(EvaluationApplication, "_request_pid_stop") as request_stop,
        patch.object(EvaluationApplication, "_kill_pid") as kill,
    ):
        with pytest.raises(RuntimeError, match="identity cannot be verified"):
            observer.cancel(evaluation_id)
        with pytest.raises(RuntimeError, match="active evaluation"):
            observer.start(
                bot_id=_instance().instance_id,
                request={
                    "kind": "comparison",
                    "profile_id": "agent-comparison-mvp",
                    "preset": "quick",
                },
            )

    request_stop.assert_not_called()
    kill.assert_not_called()
    assert observer.get(evaluation_id)["status"] == "running"
    assert evaluation_id not in observer._cancelled
    if pid_source == "claim":
        assert list(root.glob(".active-*.json"))


def test_cancel_releases_stale_pid_only_after_nonexistence_is_proven(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-exited-cancel"
    EvaluationApplication(root, validator=_ready_validator())
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
        EvaluationApplication,
        "_pid_matches_evaluation",
        return_value=True,
    ):
        observer = EvaluationApplication(root, validator=_ready_validator())
    with (
        patch.object(
            EvaluationApplication,
            "_pid_matches_evaluation",
            return_value=False,
        ),
        patch.object(
            EvaluationApplication,
            "_pid_exists",
            return_value=False,
        ),
        patch.object(EvaluationApplication, "_request_pid_stop") as request_stop,
        patch.object(EvaluationApplication, "_kill_pid") as kill,
    ):
        cancelled = observer.cancel(evaluation_id)

    request_stop.assert_not_called()
    kill.assert_not_called()
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
    manager = EvaluationApplication(
        root,
        validator=_ready_validator(captured),
    )
    with (
        patch.object(manager, "_spawn"),
        _use_manager(manager),
    ):
        response = TestClient(app).post("/api/evals/evaluations/eval-rerun-source/rerun")

    assert response.status_code == 200
    assert set(captured[0]) == {
        "kind",
        "bot_id",
        "profile_id",
        "preset",
    }
    assert response.json()["evaluation_id"] != "eval-rerun-source"


def test_suite_rerun_does_not_reuse_legacy_external_write_confirmation() -> None:
    cloned = EvaluationApplication._clone_request(
        {
            "kind": "suite",
            "bot_id": "lingye-copilot-qq",
            "suite_id": "agentstrata-capabilities-v1",
            "preset": "full",
            "case_ids": [],
            "confirm_external_write": True,
        }
    )

    assert cloned["confirm_external_write"] is False


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
    owner = EvaluationApplication(root, validator=_ready_validator())
    with owner._creation_guard(), owner._lock:
        owner._create_claim(_instance().instance_id, evaluation_id)
        owner._update_claim(
            _instance().instance_id,
            evaluation_id,
            worker_pid=4321,
        )

    with patch.object(
        EvaluationApplication,
        "_pid_matches_evaluation",
        return_value=True,
    ):
        observer = EvaluationApplication(root, validator=_ready_validator())
        with pytest.raises(RuntimeError, match="cannot be deleted"):
            observer.delete(evaluation_id)
        with pytest.raises(RuntimeError, match="active evaluation"):
            observer.clone(evaluation_id)
        with pytest.raises(RuntimeError, match="active evaluation"):
            observer.start(
                bot_id=_instance().instance_id,
                request={
                    "kind": "comparison",
                    "profile_id": "agent-comparison-mvp",
                    "preset": "quick",
                },
            )

    assert (root / evaluation_id).is_dir()


@pytest.mark.parametrize(
    "state_variant",
    ["missing", "invalid-json", "unknown-status"],
)
def test_update_readiness_fails_closed_for_invalid_lifecycle_state(
    tmp_path: Path,
    state_variant: str,
) -> None:
    root = tmp_path / "evaluations"
    application = EvaluationApplication(root, validator=_ready_validator())
    evaluation_id = "eval-update-state"
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    state_path = root / evaluation_id / "state.json"
    if state_variant == "missing":
        state_path.unlink()
    elif state_variant == "invalid-json":
        state_path.write_text("{not-json", encoding="utf-8")
        if os.name != "nt":
            state_path.chmod(0o600)
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "future-status"
        _write_json(state_path, state)

    assert application.update_readiness() == {
        "active_count": 0,
        "idle_proven": False,
    }


@pytest.mark.parametrize("pid_source", ["state", "claim"])
def test_update_readiness_rejects_terminal_record_with_verified_live_worker(
    tmp_path: Path,
    pid_source: str,
) -> None:
    root = tmp_path / "evaluations"
    application = EvaluationApplication(root, validator=_ready_validator())
    evaluation_id = "eval-update-live-terminal"
    bot_id = _instance().instance_id
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    if pid_source == "state":
        state_path = root / evaluation_id / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["pid"] = 4321
        _write_json(state_path, state)
    else:
        with application._creation_guard(), application._lock:
            application._create_claim(bot_id, evaluation_id)
            application._update_claim(
                bot_id,
                evaluation_id,
                worker_pid=4321,
            )

    with patch.object(
        EvaluationApplication,
        "_pid_matches_evaluation",
        return_value=True,
    ) as matches_worker:
        readiness = application.update_readiness()

    if pid_source == "state":
        matches_worker.assert_called()
    assert readiness == {
        "active_count": 0,
        "idle_proven": False,
    }


def test_update_readiness_distinguishes_active_count_from_uncertainty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    application = EvaluationApplication(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id="eval-update-active",
        lifecycle_status="running",
    )
    _persist_evaluation(
        root,
        evaluation_id="eval-update-unknown",
        lifecycle_status="future-status",
    )

    assert application.update_readiness() == {
        "active_count": 1,
        "idle_proven": False,
    }


def test_maintenance_enter_wins_race_with_start_validation(
    tmp_path: Path,
) -> None:
    validation_started = threading.Event()
    release_validation = threading.Event()

    def delayed_validator(
        bot: BotInstance,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        validation_started.set()
        assert release_validation.wait(timeout=5)
        return _ready_validator()(bot, request)

    root = tmp_path / "evaluations"
    application = EvaluationApplication(root, validator=delayed_validator)
    request = {
        "kind": "comparison",
        "profile_id": "agent-comparison-mvp",
        "preset": "quick",
    }
    lease_id = "a" * 32

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            application.start,
            bot_id=_instance().instance_id,
            request=request,
        )
        assert validation_started.wait(timeout=5)
        entered = application.enter_maintenance(lease_id)
        release_validation.set()
        with pytest.raises(RuntimeError, match="maintenance is active"):
            pending.result(timeout=5)

    assert entered["lease_id"] == lease_id
    assert not _evaluation_directories(root)
    assert application.leave_maintenance(lease_id) == {
        "maintenance": False,
        "lease_id": lease_id,
    }


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
    manager = EvaluationApplication(root, validator=_ready_validator())
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
    manager._monitor(evaluation_id, process, bot_id)

    assert not (root / evaluation_id).exists()
    assert evaluation_id not in manager._processes


def test_application_monitor_does_not_write_worker_log_or_progress(
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
    manager = EvaluationApplication(root, validator=_ready_validator())
    process = Mock()
    process.wait.return_value = 0
    manager._processes[evaluation_id] = process
    manager._process_bot_ids[evaluation_id] = bot_id

    directory = root / evaluation_id
    (directory / "run.log").write_text("worker-owned\n", encoding="utf-8")
    (directory / "progress.jsonl").write_text(
        '{"event":"worker-owned"}\n',
        encoding="utf-8",
    )

    manager._monitor(evaluation_id, process, bot_id)

    assert (directory / "run.log").read_text(encoding="utf-8") == "worker-owned\n"
    assert (directory / "progress.jsonl").read_text(encoding="utf-8") == (
        '{"event":"worker-owned"}\n'
    )


@pytest.mark.parametrize(
    "corruption",
    ["state-json", "result-identity", "cancel-identity"],
)
def test_worker_finalizer_keeps_claim_when_artifact_integrity_is_unknown(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    corruption: str,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-finalizer-integrity"
    bot_id = _instance().instance_id
    manager = EvaluationApplication(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="running",
    )
    directory = root / evaluation_id
    with manager._creation_guard(), manager._lock:
        manager._create_claim(bot_id, evaluation_id)
        manager._update_claim(bot_id, evaluation_id, worker_pid=4321)

    if corruption == "state-json":
        (directory / "state.json").write_text("{not-json", encoding="utf-8")
    elif corruption == "result-identity":
        result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        result["evaluation_id"] = "eval-replaced-result"
        _write_json(directory / "result.json", result)
    else:
        _write_json(
            directory / ".cancel-requested.json",
            {
                "evaluation_id": "eval-replaced-marker",
                "requested_at": "2026-07-26T00:00:00+00:00",
            },
        )

    manager._processes[evaluation_id] = Mock()
    manager._process_bot_ids[evaluation_id] = bot_id
    caplog.set_level(
        "ERROR",
        logger="chatcopilot.evals.application.controller",
    )

    manager._finalize_worker_exit(
        evaluation_id,
        bot_id=bot_id,
        exit_code=1,
    )

    assert list(root.glob(".active-*.json"))
    assert evaluation_id not in manager._processes
    assert evaluation_id not in manager._process_bot_ids
    assert "activity claim retained" in caplog.text


def test_worker_finalizer_releases_claim_only_after_terminal_state_persists(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-finalizer-write"
    bot_id = _instance().instance_id
    manager = EvaluationApplication(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    state_path = root / evaluation_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"status": "running", "finished_at": None, "pid": 4321})
    _write_json(state_path, state)
    with manager._creation_guard(), manager._lock:
        manager._create_claim(bot_id, evaluation_id)
        manager._update_claim(bot_id, evaluation_id, worker_pid=4321)

    caplog.set_level(
        "ERROR",
        logger="chatcopilot.evals.application.controller",
    )
    with patch(
        "chatcopilot.evals.application.controller._write_json",
        side_effect=OSError("simulated persistence failure"),
    ):
        manager._finalize_worker_exit(
            evaluation_id,
            bot_id=bot_id,
            exit_code=0,
        )

    assert list(root.glob(".active-*.json"))
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "running"
    assert "activity claim retained" in caplog.text

    caplog.clear()
    manager._finalize_worker_exit(
        evaluation_id,
        bot_id=bot_id,
        exit_code=0,
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "completed"
    assert persisted["pid"] is None
    assert not list(root.glob(".active-*.json"))
    assert "activity claim retained" not in caplog.text


def test_worker_finalizer_does_not_delete_replaced_cancel_marker_inode(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-finalizer-marker-race"
    bot_id = _instance().instance_id
    manager = EvaluationApplication(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    directory = root / evaluation_id
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"status": "running", "finished_at": None, "pid": 4321})
    _write_json(state_path, state)
    marker = directory / ".cancel-requested.json"
    manager._write_cancel_marker(evaluation_id)
    manager._cancelled.add(evaluation_id)
    with manager._creation_guard(), manager._lock:
        manager._create_claim(bot_id, evaluation_id)
        manager._update_claim(bot_id, evaluation_id, worker_pid=4321)

    replacement = tmp_path / "replacement-marker.json"
    replacement_payload = {
        "evaluation_id": evaluation_id,
        "requested_at": "2026-07-26T00:00:00+00:00",
        "replacement": True,
    }
    _write_json(replacement, replacement_payload)
    original_rename = os.rename
    replaced = False

    def replace_before_rename(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        nonlocal replaced
        if Path(source) == marker and not replaced:
            replaced = True
            marker.unlink()
            replacement.replace(marker)
        original_rename(source, target)

    caplog.set_level(
        "ERROR",
        logger="chatcopilot.evals.application.controller",
    )
    with patch(
        "chatcopilot.evals.application.controller.os.rename",
        side_effect=replace_before_rename,
    ):
        manager._finalize_worker_exit(
            evaluation_id,
            bot_id=bot_id,
            exit_code=0,
        )

    assert replaced is True
    assert json.loads(marker.read_text(encoding="utf-8")) == replacement_payload
    assert list(root.glob(".active-*.json"))
    assert evaluation_id in manager._cancelled
    assert "terminal cleanup failed; activity claim retained" in caplog.text


def test_cancel_marker_writer_uses_canonical_json_for_trial_guard(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-canonical-cancel-marker"
    manager = EvaluationApplication(root, validator=_ready_validator())
    directory = root / evaluation_id
    directory.mkdir(mode=0o700)

    manager._write_cancel_marker(evaluation_id)

    marker = directory / ".cancel-requested.json"
    raw = marker.read_text(encoding="utf-8")
    payload = json.loads(raw)
    expected = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    assert raw == expected
    if os.name != "nt":
        assert marker.stat().st_mode & 0o777 == 0o600


def test_get_does_not_expose_terminal_state_before_claim_release(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    evaluation_id = "eval-terminal-claim-boundary"
    bot_id = _instance().instance_id
    manager = EvaluationApplication(root, validator=_ready_validator())
    _persist_evaluation(
        root,
        evaluation_id=evaluation_id,
        lifecycle_status="completed",
    )
    state_path = root / evaluation_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"status": "running", "pid": 4321, "finished_at": None})
    _write_json(state_path, state)
    with manager._creation_guard(), manager._lock:
        manager._create_claim(bot_id, evaluation_id)
        manager._update_claim(bot_id, evaluation_id, worker_pid=4321)

    release_entered = threading.Event()
    allow_release = threading.Event()
    original_release = manager._release_claim

    def blocking_release(claim_bot_id: str, claim_evaluation_id: str) -> None:
        release_entered.set()
        assert allow_release.wait(timeout=5)
        original_release(claim_bot_id, claim_evaluation_id)

    with (
        patch.object(manager, "_release_claim", side_effect=blocking_release),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        finalizing = executor.submit(
            manager._finalize_worker_exit,
            evaluation_id,
            bot_id=bot_id,
            exit_code=0,
        )
        assert release_entered.wait(timeout=5)
        reading = executor.submit(manager.get, evaluation_id)
        time.sleep(0.05)
        assert not reading.done()
        allow_release.set()
        finalizing.result(timeout=5)
        detail = reading.result(timeout=5)

    assert detail["status"] == "completed"
    assert not list(root.glob(".active-*.json"))


def test_application_exposes_no_console_shutdown_hook(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evaluations"
    manager = EvaluationApplication(root, validator=_ready_validator())

    assert not hasattr(manager, "close")


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
    summary_path = directory / "summary.md"
    summary_path.write_text(
        "# Evaluation\n",
        encoding="utf-8",
    )
    progress_path = directory / "progress.jsonl"
    progress_path.write_text(
        json.dumps(
            {
                "event": "trial_completed",
                "case_ref": "ifeval:ifeval-json-format",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        summary_path.chmod(0o600)
        progress_path.chmod(0o600)

    manager = EvaluationApplication(root)
    with _use_manager(manager):
        client = TestClient(app)
        case = client.get(f"/api/evals/evaluations/{evaluation_id}/cases/ifeval:ifeval-json-format")
        stream = client.get(f"/api/evals/evaluations/{evaluation_id}/stream")
        export = client.get(f"/api/evals/evaluations/{evaluation_id}/export/markdown")
        removed = client.delete(f"/api/evals/evaluations/{evaluation_id}")

    assert case.status_code == 200
    assert case.json()["trials"][0]["outcome"] == "passed"
    assert stream.status_code == 200
    assert '"event": "trial_completed"' in stream.text
    assert '"status": "completed"' in stream.text
    assert export.status_code == 200
    assert export.text == "# Evaluation\n"
    assert removed.status_code == 200
    assert not directory.exists()
