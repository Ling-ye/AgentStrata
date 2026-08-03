from __future__ import annotations

import json
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

import chatcopilot.evals.evaluations as evaluation_module
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.evals.cli import main as evals_cli_main
from chatcopilot.evals.artifact_ids import trial_artifact_id
from chatcopilot.evals.evaluations import (
    EvaluationTarget,
    EvaluationTrial,
    TrialExecutionRequest,
    aggregate_comparison,
    execute_evaluation_trial,
    parse_evaluation_request,
    run_evaluation,
    validate_evaluation,
)
from chatcopilot.evals.isolated_executor import (
    judge_profile_trial,
    permission_filter,
    stage_fixture,
)
from chatcopilot.evals.models import EvalCase, EvalCaseResult, EvalRunResult
from chatcopilot.evals.profiles import get_profile
from chatcopilot.evals.redaction import (
    collect_env_secrets,
    redact_payload,
    sanitize_text,
)


@pytest.fixture(autouse=True)
def _available_codex_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CHATCOPILOT_LINGYE_API_KEY",
        "test-" + "credential",
    )
    monkeypatch.setattr(
        "chatcopilot.evals.evaluations.shutil.which",
        lambda _binary: "/usr/bin/codex",
    )


def _custom_request(
    *,
    evaluation_id: str,
    targets: list[str] | None = None,
    case_refs: list[str] | None = None,
    repetitions: int = 1,
    max_wall_seconds: float = 30,
    seed: int = 19,
) -> dict:
    profile = get_profile("agent-comparison-mvp")
    return {
        "evaluation_id": evaluation_id,
        "kind": "comparison",
        "bot": "lingye-copilot-qq",
        "profile": profile.profile_id,
        "preset": "custom",
        "targets": targets or ["codex", "native"],
        "case_refs": case_refs or [profile.cases[0].ref],
        "repetitions": repetitions,
        "max_wall_seconds": max_wall_seconds,
        "seed": seed,
    }


def _trial(
    request: TrialExecutionRequest,
    *,
    outcome: str = "passed",
    score: float = 1.0,
) -> EvaluationTrial:
    return EvaluationTrial(
        trial_id=(
            f"{request.case.case_id}-a{request.attempt}-"
            f"{request.target.fingerprint[:12]}"
        ),
        evaluation_id=request.evaluation_id,
        kind=request.kind,
        bot=request.bot,
        profile=request.profile,
        suite_id=request.suite_id,
        case_ref=(
            request.profile_case.ref
            if request.profile_case is not None
            else f"{request.suite_id}:{request.case.case_id}"
        ),
        case_id=request.case.case_id,
        dimension=request.dimension,
        target_id=request.target.target_id,
        target_fingerprint=request.target.fingerprint,
        executor=request.target.executor,
        backend=request.target.backend,
        model=request.target.model,
        reasoning_effort=request.target.reasoning_effort,
        attempt=request.attempt,
        order=request.order,
        outcome=outcome,  # type: ignore[arg-type]
        score=score,
        max_score=1.0,
        passed=outcome == "passed",
        started_at="2026-07-26T00:00:00+00:00",
        finished_at="2026-07-26T00:00:01+00:00",
    )


def _create_resumable_evaluation(
    request: dict,
    output: Path,
) -> None:
    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    result = run_evaluation(
        request,
        output=output,
        cancel_check=cancel,
        trial_executor=_trial,
    )
    assert result.status == "cancelled"


def _write_comparison_bootstrap(
    request: dict,
    output: Path,
    *,
    request_overrides: dict | None = None,
    state_overrides: dict | None = None,
    run_log: str = "",
) -> None:
    parsed = parse_evaluation_request(request)
    assert parsed.kind == "comparison"
    validation = validate_evaluation(request)
    bot_spec = str(request["bot"])
    bot_path = Path(bot_spec)
    bot_id = (
        bot_path.parent.name
        if bot_path.name == "bot.yaml"
        else bot_path.name
    )
    stored_request = {
        "evaluation_id": parsed.evaluation_id,
        "kind": parsed.kind,
        "bot_id": bot_id,
        "bot_spec": bot_spec,
        "created_at": "2026-07-26T00:00:00+00:00",
        "profile_id": parsed.profile,
        "preset": parsed.preset,
        "target_ids": list(parsed.targets),
        "case_refs": list(parsed.case_refs),
        "repetitions": parsed.repetitions,
        "max_wall_seconds": parsed.max_wall_seconds,
        "seed": parsed.seed,
        "targets": validation["targets"],
        **(request_overrides or {}),
    }
    output.mkdir()
    (output / "request.json").write_text(
        json.dumps(stored_request),
        encoding="utf-8",
    )
    (output / "state.json").write_text(
        json.dumps(
            {
                "evaluation_id": parsed.evaluation_id,
                "kind": parsed.kind,
                "status": "running",
                "pid": 123,
                "started_at": "2026-07-26T00:00:01+00:00",
                "finished_at": None,
                "duration_seconds": None,
                "completed_trials": 0,
                "planned_trials": (
                    len(parsed.case_refs)
                    * parsed.repetitions
                    * len(parsed.targets)
                ),
                "error": None,
                **(state_overrides or {}),
            }
        ),
        encoding="utf-8",
    )
    (output / "run.log").write_text(run_log, encoding="utf-8")


def test_strict_quick_request_rejects_all_creation_overrides() -> None:
    request = {
        "kind": "comparison",
        "bot": "lingye-copilot-qq",
        "profile": "agent-comparison-mvp",
        "preset": "quick",
        "targets": ["codex", "native"],
        "case_refs": ["ifeval:ifeval-json-format"],
        "repetitions": 1,
        "max_wall_seconds": 30,
        "seed": 7,
    }

    result = validate_evaluation(request)

    assert result["ready"] is False
    assert result["checks"][0]["code"] == "request"
    assert "does not accept overrides" in result["checks"][0]["detail"]
    assert isinstance(result["checks"], list)


def test_fresh_run_rejects_nonempty_output_without_modifying_it(
    tmp_path: Path,
) -> None:
    output = tmp_path / "eval-stale-output"
    output.mkdir()
    stale = output / "result.json"
    stale.write_text('{"stale":true}\n', encoding="utf-8")
    before = stale.read_bytes()

    with pytest.raises(ValueError, match="not a Console bootstrap"):
        run_evaluation(
            _custom_request(evaluation_id="eval-stale-output"),
            output=output,
            trial_executor=_trial,
        )

    assert stale.read_bytes() == before
    assert list(output.iterdir()) == [stale]


@pytest.mark.parametrize(
    ("overrides", "run_log", "message"),
    (
        ({"bot_spec": "bots/other/bot.yaml"}, "", "bot_spec"),
        ({"bot_id": "other-bot"}, "", "bot_id"),
        ({}, "stale output", "run.log must be empty"),
    ),
)
def test_fresh_run_rejects_forged_or_used_console_bootstrap(
    tmp_path: Path,
    overrides: dict,
    run_log: str,
    message: str,
) -> None:
    request = _custom_request(evaluation_id="eval-forged-bootstrap")
    request["bot"] = "bots/lingye-copilot-qq/bot.yaml"
    output = tmp_path / "eval-forged-bootstrap"
    _write_comparison_bootstrap(
        request,
        output,
        request_overrides=overrides,
        run_log=run_log,
    )
    before = {
        path.name: path.read_bytes()
        for path in output.iterdir()
    }

    with pytest.raises(ValueError, match=message):
        run_evaluation(request, output=output, trial_executor=_trial)

    assert {
        path.name: path.read_bytes()
        for path in output.iterdir()
    } == before


@pytest.mark.parametrize(
    ("request_overrides", "state_overrides", "message"),
    (
        ({"unexpected": True}, {}, "request fields"),
        ({}, {"unexpected": True}, "state fields"),
        ({}, {"completed_trials": 1}, "state does not match"),
        ({}, {"error": "forged"}, "state does not match"),
    ),
)
def test_fresh_run_requires_exact_console_bootstrap_shape(
    tmp_path: Path,
    request_overrides: dict,
    state_overrides: dict,
    message: str,
) -> None:
    request = _custom_request(evaluation_id="eval-strict-bootstrap")
    request["bot"] = "bots/lingye-copilot-qq/bot.yaml"
    output = tmp_path / "eval-strict-bootstrap"
    _write_comparison_bootstrap(
        request,
        output,
        request_overrides=request_overrides,
        state_overrides=state_overrides,
    )
    before = {
        path.name: path.read_bytes()
        for path in output.iterdir()
    }

    with pytest.raises(ValueError, match=message):
        run_evaluation(request, output=output, trial_executor=_trial)

    assert {
        path.name: path.read_bytes()
        for path in output.iterdir()
    } == before


@pytest.mark.parametrize(
    "evaluation_id",
    ("contains.dot", "x" * 129),
)
def test_core_evaluation_id_matches_console_grammar(evaluation_id: str) -> None:
    with pytest.raises(ValueError, match="evaluation_id"):
        parse_evaluation_request(
            _custom_request(evaluation_id=evaluation_id)
        )


def test_profile_cases_are_stable_when_official_data_env_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATCOPILOT_IFEVAL_DATA_PATH", "/missing/official.jsonl")
    profile = get_profile("agent-comparison-mvp")

    assert [item.ref for item in profile.cases] == [
        "ifeval:ifeval-json-format",
        "gaia-smoke:gaia-smoke-arithmetic",
        "agent-comparison:comparison-tool-lookup",
        "agent-comparison:comparison-code-multiply",
    ]
    assert all(item.case.metadata["source"] == "profile-snapshot-v1" for item in profile.cases)


def test_validation_exposes_fingerprinted_executor_targets() -> None:
    result = validate_evaluation(
        {
            "kind": "comparison",
            "bot": "lingye-copilot-qq",
            "profile": "agent-comparison-mvp",
            "preset": "quick",
        }
    )

    assert result["ready"] is True
    assert [item["executor"] for item in result["targets"]] == [
        "agent_isolated",
        "agent_isolated",
    ]
    assert all(len(item["fingerprint"]) == 64 for item in result["targets"])


@pytest.mark.parametrize(
    ("suite", "expected_executor", "llm_judge"),
    (
        ("bfcl", "direct_llm", False),
        ("ifeval", "agent_configured", False),
        ("gaia", "agent_configured", True),
    ),
)
def test_suite_validation_selects_explicit_executor_policy(
    suite: str,
    expected_executor: str,
    llm_judge: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = EvalCase(
        case_id=f"{suite}-case",
        input="question",
        category="test",
        expected_behavior="answer",
        metadata={"adapter": suite},
    )
    monkeypatch.setattr(
        "chatcopilot.evals.evaluations.get_cases",
        lambda _suite, *, auto_prepare: (case,),
    )

    result = validate_evaluation(
        {
            "kind": "suite",
            "bot": "lingye-copilot-qq",
            "suite": suite,
            "case_ids": [case.case_id],
            "dry_run": False,
            "llm_judge": llm_judge,
        }
    )

    assert result["ready"] is True
    assert result["targets"][0]["executor"] == expected_executor


def test_dry_run_validation_selects_dry_run_executor() -> None:
    result = validate_evaluation(
        {
            "kind": "suite",
            "suite": "ifeval",
            "case_ids": ["ifeval-json-format"],
            "dry_run": True,
            "llm_judge": False,
        }
    )

    assert result["ready"] is True
    assert result["targets"][0]["executor"] == "dry_run"


def test_orchestrator_uses_seeded_complete_target_groups_and_canonical_state(
    tmp_path: Path,
) -> None:
    profile = get_profile("agent-comparison-mvp")
    seen: list[tuple[str, int, str]] = []
    output = tmp_path / "eval-seeded"

    def execute(request: TrialExecutionRequest) -> EvaluationTrial:
        seen.append((request.profile_case.ref, request.attempt, request.target.target_id))  # type: ignore[union-attr]
        return _trial(
            request,
            score=1.0 if request.target.target_id == "codex" else 0.0,
            outcome="passed" if request.target.target_id == "codex" else "failed",
        )

    result = run_evaluation(
        _custom_request(
            evaluation_id="eval-seeded",
            case_refs=[profile.cases[0].ref, profile.cases[1].ref],
            repetitions=2,
            seed=19,
        ),
        output=output,
        trial_executor=execute,
    )

    groups = [seen[index : index + 2] for index in range(0, len(seen), 2)]
    assert all({item[2] for item in group} == {"codex", "native"} for group in groups)
    assert [group[0][2] for group in groups] == ["native", "codex", "native", "codex"]
    assert result.status == "completed"
    assert len(result.trials) == 8
    state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    assert state["evaluation_id"] == "eval-seeded"
    assert state["status"] == "completed"
    assert state["pid"] is None
    assert "outcome" in json.loads(
        next((output / "trials").glob("*.json")).read_text(encoding="utf-8")
    )


def test_budget_stops_only_before_next_complete_target_group(tmp_path: Path) -> None:
    calls = 0
    output = tmp_path / "eval-budget"

    def execute(request: TrialExecutionRequest) -> EvaluationTrial:
        nonlocal calls
        calls += 1
        time.sleep(0.015)
        return _trial(request)

    result = run_evaluation(
        _custom_request(
            evaluation_id="eval-budget",
            repetitions=3,
            max_wall_seconds=0.01,
        ),
        output=output,
        trial_executor=execute,
    )

    assert result.status == "partial"
    assert calls == 2
    assert len(result.trials) == 2
    assert result.summary["paired_attempt_count"] == 1


def test_cancel_and_resume_operate_only_at_complete_target_group_boundaries(
    tmp_path: Path,
) -> None:
    calls: list[tuple[int, str]] = []
    cancel_checks = 0

    def execute(request: TrialExecutionRequest) -> EvaluationTrial:
        calls.append((request.attempt, request.target.target_id))
        return _trial(request)

    def cancel() -> bool:
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks > 1

    request = _custom_request(
        evaluation_id="eval-resume",
        repetitions=2,
    )
    output = tmp_path / "eval-resume"
    cancelled = run_evaluation(
        request,
        output=output,
        cancel_check=cancel,
        trial_executor=execute,
    )

    assert cancelled.status == "cancelled"
    assert len(cancelled.trials) == 2
    assert {target for _attempt, target in calls} == {"codex", "native"}

    calls.clear()
    resumed = run_evaluation(
        request,
        output=output,
        trial_executor=execute,
        resume=True,
    )

    assert resumed.status == "completed"
    assert len(resumed.trials) == 4
    assert calls == [(2, "codex"), (2, "native")]
    assert resumed.started_at == cancelled.started_at
    assert resumed.duration_seconds >= cancelled.duration_seconds


def test_resume_rejects_request_drift_without_touching_artifacts(
    tmp_path: Path,
) -> None:
    output = tmp_path / "eval-resume-drift"
    request = _custom_request(
        evaluation_id="eval-resume-drift",
        repetitions=2,
    )
    _create_resumable_evaluation(request, output)
    before = {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ValueError, match="request_hash"):
        run_evaluation(
            {**request, "seed": request["seed"] + 1},
            output=output,
            trial_executor=_trial,
            resume=True,
        )

    after = {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_resume_rejects_changed_case_snapshot_without_touching_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "eval-resume-case-drift"
    request = _custom_request(
        evaluation_id="eval-resume-case-drift",
        repetitions=2,
    )
    _create_resumable_evaluation(request, output)
    request_before = (output / "request.json").read_bytes()
    profile = get_profile("agent-comparison-mvp")
    changed_case = replace(
        profile.cases[0],
        case=replace(profile.cases[0].case, input="changed immutable case input"),
    )
    changed_profile = replace(
        profile,
        cases=(changed_case, *profile.cases[1:]),
    )
    monkeypatch.setattr(
        evaluation_module,
        "get_profile",
        lambda _profile_id: changed_profile,
    )

    with pytest.raises(ValueError, match="case_hash"):
        run_evaluation(
            request,
            output=output,
            trial_executor=_trial,
            resume=True,
        )

    assert (output / "request.json").read_bytes() == request_before


def test_resume_rejects_changed_target_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "eval-resume-target-drift"
    request = _custom_request(
        evaluation_id="eval-resume-target-drift",
        repetitions=2,
    )
    _create_resumable_evaluation(request, output)
    original = evaluation_module._isolated_target

    def changed_target(
        target_id: str,
        config: object,
        *,
        config_fingerprint: str,
    ) -> tuple:
        target, check = original(
            target_id,
            config,
            config_fingerprint=config_fingerprint,
        )
        return replace(target, fingerprint="f" * 64), check

    monkeypatch.setattr(evaluation_module, "_isolated_target", changed_target)

    with pytest.raises(ValueError, match="target_fingerprints"):
        run_evaluation(
            request,
            output=output,
            trial_executor=_trial,
            resume=True,
        )


def test_resume_rejects_runtime_prompt_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "eval-runtime-drift"
    request = _custom_request(
        evaluation_id="eval-runtime-drift",
        repetitions=2,
    )
    _create_resumable_evaluation(request, output)
    original = evaluation_module.load_evaluation_runtime

    def changed_runtime(bot: str) -> object:
        runtime = original(bot)
        return replace(
            runtime,
            system_prompt=runtime.system_prompt + "\nchanged evaluation behavior",
        )

    monkeypatch.setattr(
        evaluation_module,
        "load_evaluation_runtime",
        changed_runtime,
    )

    with pytest.raises(ValueError, match="target_fingerprints"):
        run_evaluation(
            request,
            output=output,
            trial_executor=_trial,
            resume=True,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "message"),
    (
        ("evaluation_id", "other-evaluation", "Evaluation identity"),
        ("kind", "suite", "Evaluation identity"),
        ("case_ref", "ifeval:other-case", "Case identity"),
        ("case_id", "other-case", "Case identity"),
        ("attempt", 99, "attempt"),
        ("target_id", "other-target", "Target fingerprint"),
        ("target_fingerprint", "0" * 64, "Target fingerprint"),
        ("order", 99, "order"),
        ("trial_id", "forged-trial", "id is invalid"),
        ("outcome", "completed", "outcome"),
    ),
)
def test_resume_rejects_corrupt_trial_identity_without_writing(
    tmp_path: Path,
    field_name: str,
    invalid_value: object,
    message: str,
) -> None:
    output = tmp_path / "eval-corrupt-trial"
    request = _custom_request(
        evaluation_id="eval-corrupt-trial",
        repetitions=2,
    )
    _create_resumable_evaluation(request, output)
    result_path = output / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["trials"][0][field_name] = invalid_value
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    before = result_path.read_bytes()

    with pytest.raises(ValueError, match=message):
        run_evaluation(
            request,
            output=output,
            trial_executor=_trial,
            resume=True,
        )

    assert result_path.read_bytes() == before


def test_resume_rejects_duplicate_or_incomplete_target_groups(
    tmp_path: Path,
) -> None:
    request = _custom_request(
        evaluation_id="eval-corrupt-groups",
        repetitions=2,
    )
    for mutation, message in (
        ("duplicate", "duplicated"),
        ("incomplete", "incomplete Target group"),
    ):
        output = tmp_path / mutation
        _create_resumable_evaluation(request, output)
        result_path = output / "result.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if mutation == "duplicate":
            payload["trials"].append(dict(payload["trials"][0]))
        else:
            payload["trials"].pop()
        result_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match=message):
            run_evaluation(
                request,
                output=output,
                trial_executor=_trial,
                resume=True,
            )


def test_resume_rejects_non_finite_result_payload(tmp_path: Path) -> None:
    output = tmp_path / "eval-non-finite-result"
    request = _custom_request(
        evaluation_id="eval-non-finite-result",
        repetitions=2,
    )
    _create_resumable_evaluation(request, output)
    result_path = output / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["summary"]["poison"] = float("nan")
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="non-finite"):
        run_evaluation(
            request,
            output=output,
            trial_executor=_trial,
            resume=True,
        )


def test_completed_evaluation_cannot_resume(tmp_path: Path) -> None:
    output = tmp_path / "eval-completed-resume"
    request = _custom_request(evaluation_id="eval-completed-resume")
    completed = run_evaluation(
        request,
        output=output,
        trial_executor=_trial,
    )

    with pytest.raises(ValueError, match="completed Evaluation"):
        run_evaluation(
            request,
            output=output,
            trial_executor=_trial,
            resume=True,
        )

    assert completed.status == "completed"


def test_resume_budget_is_cumulative(tmp_path: Path) -> None:
    output = tmp_path / "eval-cumulative-budget"
    request = _custom_request(
        evaluation_id="eval-cumulative-budget",
        repetitions=3,
        max_wall_seconds=0.01,
    )

    def slow_trial(trial_request: TrialExecutionRequest) -> EvaluationTrial:
        time.sleep(0.015)
        return _trial(trial_request)

    partial = run_evaluation(
        request,
        output=output,
        trial_executor=slow_trial,
    )
    resumed_calls = 0

    def resumed_trial(trial_request: TrialExecutionRequest) -> EvaluationTrial:
        nonlocal resumed_calls
        resumed_calls += 1
        return _trial(trial_request)

    resumed = run_evaluation(
        request,
        output=output,
        trial_executor=resumed_trial,
        resume=True,
    )

    assert partial.status == "partial"
    assert resumed.status == "partial"
    assert resumed_calls == 0
    assert resumed.started_at == partial.started_at
    assert resumed.duration_seconds >= partial.duration_seconds


def test_resume_cleans_uncheckpointed_trial_workspaces(tmp_path: Path) -> None:
    output = tmp_path / "eval-clean-workspace"
    request = _custom_request(
        evaluation_id="eval-clean-workspace",
        repetitions=2,
    )
    _create_resumable_evaluation(request, output)
    payload = json.loads((output / "result.json").read_text(encoding="utf-8"))
    profile = get_profile("agent-comparison-mvp")
    case_id = profile.cases[0].case_id
    markers: list[Path] = []
    for target in payload["targets"]:
        trial_id = trial_artifact_id(
            case_id,
            attempt=2,
            target_fingerprint=target["fingerprint"],
        )
        workspace = output / "workspaces" / trial_id
        workspace.mkdir(parents=True, exist_ok=True)
        marker = workspace / "stale.marker"
        marker.write_text("stale", encoding="utf-8")
        markers.append(marker)

    def execute(trial_request: TrialExecutionRequest) -> EvaluationTrial:
        trial_id = trial_artifact_id(
            trial_request.case.case_id,
            attempt=trial_request.attempt,
            target_fingerprint=trial_request.target.fingerprint,
        )
        assert not (output / "workspaces" / trial_id / "stale.marker").exists()
        return _trial(trial_request)

    resumed = run_evaluation(
        request,
        output=output,
        trial_executor=execute,
        resume=True,
    )

    assert resumed.status == "completed"
    assert all(not marker.exists() for marker in markers)


@pytest.mark.parametrize("artifact_name", ("progress.jsonl", "trials"))
def test_resume_rejects_artifact_symlinks(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    output = tmp_path / f"eval-symlink-{artifact_name.replace('.', '-')}"
    request = _custom_request(
        evaluation_id=f"eval-symlink-{artifact_name.replace('.', '-')}",
        repetitions=2,
    )
    _create_resumable_evaluation(request, output)
    target = output / artifact_name
    if target.is_dir():
        shutil.rmtree(target)
        outside = tmp_path / f"outside-{artifact_name}"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    else:
        target.unlink()
        target.symlink_to(output / "result.json")
    result_before = (output / "result.json").read_bytes()

    with pytest.raises(ValueError, match="symlink"):
        run_evaluation(
            request,
            output=output,
            trial_executor=_trial,
            resume=True,
        )

    assert (output / "result.json").read_bytes() == result_before


def test_completed_lifecycle_is_independent_from_failed_trial_outcome(
    tmp_path: Path,
) -> None:
    output = tmp_path / "eval-failed-outcome"
    result = run_evaluation(
        _custom_request(evaluation_id="eval-failed-outcome"),
        output=output,
        trial_executor=lambda request: _trial(
            request,
            outcome="failed",
            score=0.0,
        ),
    )

    assert result.status == "completed"
    assert {trial.outcome for trial in result.trials} == {"failed"}
    assert result.summary["outcomes"]["failed"] == 2


def test_single_target_does_not_generate_a_fake_win(tmp_path: Path) -> None:
    output = tmp_path / "eval-single-target"
    result = run_evaluation(
        _custom_request(
            evaluation_id="eval-single-target",
            targets=["native"],
        ),
        output=output,
        trial_executor=lambda request: _trial(request),
    )

    assert result.status == "completed"
    assert result.comparisons[0].verdict == "not_applicable"
    assert result.summary["wins"] == {}
    assert result.summary["paired_attempt_count"] == 0


def test_dimension_aggregation_accumulates_multiple_cases() -> None:
    profile = get_profile("agent-comparison-mvp")
    cases = (
        profile.cases[0],
        replace(profile.cases[1], dimension=profile.cases[0].dimension),
    )
    targets = (
        EvaluationTarget(
            "codex",
            "Codex",
            "agent_isolated",
            "codex",
            "model",
            "medium",
            "a" * 64,
        ),
        EvaluationTarget(
            "native",
            "Native",
            "agent_isolated",
            "native",
            "model",
            "",
            "b" * 64,
        ),
    )
    trials: list[EvaluationTrial] = []
    for case in cases:
        for target in targets:
            trials.append(
                EvaluationTrial(
                    trial_id=f"{case.case_id}-{target.target_id}",
                    evaluation_id="eval-aggregate",
                    kind="comparison",
                    bot="bot",
                    profile=profile.profile_id,
                    suite_id=case.suite_id,
                    case_ref=case.ref,
                    case_id=case.case_id,
                    dimension=case.dimension,
                    target_id=target.target_id,
                    target_fingerprint=target.fingerprint,
                    executor=target.executor,
                    backend=target.backend,
                    model=target.model,
                    reasoning_effort=target.reasoning_effort,
                    attempt=1,
                    order=1,
                    outcome="passed",
                    score=1.0,
                    passed=True,
                )
            )

    _comparisons, dimensions, summary = aggregate_comparison(trials, cases, targets)

    dimension = dimensions[profile.cases[0].dimension]
    assert dimension["case_count"] == 2
    assert dimension["sample_size"] == 2
    assert dimension["targets"]["codex"]["attempt_count"] == 2
    assert summary["paired_attempt_count"] == 2


def test_persistence_redacts_secrets_and_preserves_console_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_API_KEY", "super-secret-value")
    output = tmp_path / "eval-redacted"
    request = _custom_request(evaluation_id="eval-redacted")
    request["bot"] = "bots/lingye-copilot-qq/bot.yaml"
    _write_comparison_bootstrap(request, output)

    def execute(request: TrialExecutionRequest) -> EvaluationTrial:
        return replace(
            _trial(request),
            final_text=f"super-secret-value at {request.output}",
            events=(
                {"type": "ToolFinished", "summary": "api_key=super-secret-value"},
            ),
        )

    run_evaluation(
        request,
        output=output,
        trial_executor=execute,
    )
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in output.rglob("*")
        if path.is_file() and path.suffix in {".json", ".md"}
    )
    request_payload = json.loads((output / "request.json").read_text(encoding="utf-8"))

    assert request_payload["bot_id"] == "lingye-copilot-qq"
    assert request_payload["created_at"] == "2026-07-26T00:00:00+00:00"
    assert "super-secret-value" not in persisted
    assert str(output) not in persisted
    assert "[REDACTED]" in persisted


def test_redaction_covers_generic_tokens_inline_secrets_and_absolute_paths() -> None:
    hf_token = "hf-secret-value"
    sample_token = "sample-secret-value"
    secrets = collect_env_secrets(
        {
            "CHATCOPILOT_HF_TOKEN": hf_token,
            "SAMPLE_API_TOKEN": sample_token,
            "TOKENIZER_PARALLELISM": "false",
        }
    )
    private_home_path = "/home/" + "user/private"
    payload = redact_payload(
        {
            "hf_token": hf_token,
            "message": (
                f"SAMPLE_API_TOKEN={sample_token} "
                "at /opt/private/model.bin and D:\\private\\model.bin "
                f"from https://example.test/docs/path and error:{private_home_path}"
            ),
        },
        secrets=secrets,
    )
    encoded = json.dumps(payload)

    assert hf_token not in encoded
    assert sample_token not in encoded
    assert "/opt/private/model.bin" not in encoded
    assert "D:\\\\private\\\\model.bin" not in encoded
    assert private_home_path not in encoded
    assert "https://example.test/docs/path" in encoded
    assert encoded.count("$ABSOLUTE_PATH") == 3
    assert sanitize_text("token=abc12345") == "token=[REDACTED]"


def test_external_case_id_cannot_escape_trial_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = EvalCase(
        case_id="../../escaped",
        input="prompt",
        category="instruction_following",
        expected_behavior="answer",
        metadata={"adapter": "ifeval"},
    )
    monkeypatch.setattr(
        evaluation_module,
        "get_cases",
        lambda _suite, *, auto_prepare: (case,),
    )
    output = tmp_path / "evaluation"
    result = run_evaluation(
        {
            "evaluation_id": "eval-safe-case-id",
            "kind": "suite",
            "suite": "ifeval",
            "case_ids": [case.case_id],
            "dry_run": True,
            "llm_judge": False,
        },
        output=output,
        trial_executor=_trial,
    )

    assert result.trials[0].case_id == "../../escaped"
    assert "/" not in result.trials[0].trial_id
    assert ".." not in result.trials[0].trial_id
    assert len(list((output / "trials").glob("*.json"))) == 1
    assert not (tmp_path / "escaped-a1").exists()


def test_external_case_id_cannot_escape_suite_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = EvalCase(
        case_id="../../../outside",
        input="prompt",
        category="instruction_following",
        expected_behavior="answer",
        metadata={"adapter": "ifeval"},
    )
    target = EvaluationTarget(
        target_id="dry-run",
        label="Dry Run",
        executor="dry_run",
        backend="none",
        model="",
        reasoning_effort="",
        fingerprint="a" * 64,
    )
    workspace_roots: list[Path] = []

    def fake_run_suite(
        _suite_id: str,
        **kwargs: object,
    ) -> EvalRunResult:
        workspace_roots.append(Path(kwargs["workspace_root"]))  # type: ignore[arg-type]
        return EvalRunResult(
            suite_id="ifeval",
            bot=None,
            status="skipped",
            started_at="2026-07-26T00:00:00+00:00",
            duration_seconds=0.0,
            cases=(
                EvalCaseResult(
                    case_id=case.case_id,
                    suite_id="ifeval",
                    status="skipped",
                ),
            ),
        )

    monkeypatch.setattr(evaluation_module, "run_suite", fake_run_suite)
    output = tmp_path / "evaluation"
    execute_evaluation_trial(
        TrialExecutionRequest(
            evaluation_id="eval-safe-workspace",
            kind="suite",
            bot="",
            output=output,
            suite_id="ifeval",
            profile="",
            profile_case=None,
            case=case,
            dimension=case.category,
            target=target,
            attempt=1,
            order=1,
            dry_run=True,
        )
    )

    assert len(workspace_roots) == 1
    assert workspace_roots[0].is_relative_to(output / "workspaces")


def test_suite_workspace_rejects_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "evaluation"
    outside = tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    (output / "workspaces").symlink_to(outside, target_is_directory=True)
    case = EvalCase(
        case_id="safe-case",
        input="prompt",
        category="instruction_following",
        expected_behavior="answer",
    )
    target = EvaluationTarget(
        target_id="dry-run",
        label="Dry Run",
        executor="dry_run",
        backend="none",
        model="",
        reasoning_effort="",
        fingerprint="a" * 64,
    )
    monkeypatch.setattr(
        evaluation_module,
        "run_suite",
        lambda *_args, **_kwargs: pytest.fail("executor must not run"),
    )

    with pytest.raises(ValueError, match="escapes"):
        execute_evaluation_trial(
            TrialExecutionRequest(
                evaluation_id="eval-symlink-escape",
                kind="suite",
                bot="",
                output=output,
                suite_id="ifeval",
                profile="",
                profile_case=None,
                case=case,
                dimension=case.category,
                target=target,
                attempt=1,
                order=1,
                dry_run=True,
            )
        )


def test_suite_dry_run_uses_unified_result_and_skipped_outcome(
    tmp_path: Path,
) -> None:
    request = {
        "evaluation_id": "eval-suite-dry",
        "kind": "suite",
        "suite": "ifeval",
        "case_ids": ["ifeval-json-format"],
        "dry_run": True,
        "llm_judge": False,
    }

    output = tmp_path / "eval-suite-dry"
    result = run_evaluation(request, output=output)

    assert result.kind == "suite"
    assert result.status == "completed"
    assert result.selected_cases == ("ifeval:ifeval-json-format",)
    assert result.trials[0].executor == "dry_run"
    assert result.trials[0].outcome == "skipped"
    assert result.comparisons == ()
    events = [
        json.loads(line)
        for line in (output / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["event"] == "evaluation_started"
    assert events[-1]["event"] == "evaluation_completed"
    assert not (output / "progress.json").exists()


def test_cli_json_stdout_remains_one_parseable_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "eval-json-output"

    exit_code = evals_cli_main(
        [
            "run",
            "--suite",
            "ifeval",
            "--dry-run",
            "--case-id",
            "ifeval-json-format",
            "--output",
            str(output),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["evaluation_id"] == "eval-json-output"
    assert "__EVAL_EVENT__" not in captured.out
    assert "__EVAL_EVENT__" in captured.err
    stored = json.loads((output / "request.json").read_text(encoding="utf-8"))
    assert stored["targets"][0]["executor"] == "dry_run"
    assert stored["core_request"]["evaluation_id"] == "eval-json-output"

    rerun_code = evals_cli_main(
        [
            "run",
            "--request",
            str(output / "request.json"),
            "--output",
            str(output),
            "--resume",
            "--json",
        ]
    )
    rerun = capsys.readouterr()

    assert rerun_code == 2
    assert json.loads(rerun.err)["code"] == "evaluation_resume_rejected"
    assert "completed Evaluation cannot be resumed" in rerun.err


def test_cli_prepare_uses_official_data_preparer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "chatcopilot.evals.cli.prepare_official_data",
        lambda suite: {"suite_id": suite, "ready": True, "path": "/data/ifeval"},
    )

    exit_code = evals_cli_main(["prepare", "--suite", "ifeval", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["suite_id"] == "ifeval"


def test_cli_rejects_mixed_request_sources() -> None:
    with pytest.raises(SystemExit):
        evals_cli_main(
            [
                "run",
                "--request",
                '{"kind":"suite","suite":"ifeval","dry_run":true}',
                "--suite",
                "ifeval",
                "--validate-only",
            ]
        )


@pytest.mark.parametrize(
    "payload",
    (
        {
            "kind": "suite",
            "suite": "ifeval",
            "dry_run": "false",
            "llm_judge": False,
        },
        {
            "kind": "suite",
            "suite": "ifeval",
            "case_ids": "ifeval-json-format",
            "dry_run": True,
            "llm_judge": False,
        },
    ),
)
def test_cli_request_rejects_boolean_and_list_coercion(
    payload: dict,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = evals_cli_main(
        ["run", "--request", json.dumps(payload), "--validate-only"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "must be a boolean" in captured.out or "must be a non-empty list" in captured.out


def test_cli_request_omitted_case_ids_selects_all_cases(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = evals_cli_main(
        [
            "run",
            "--request",
            '{"kind":"suite","suite":"ifeval","dry_run":true,"llm_judge":false}',
            "--validate-only",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["effective_request"]["case_ids"] == []
    assert next(item for item in payload["checks"] if item["code"] == "suite")["ok"]


def test_cli_resume_rejection_is_structured_and_has_no_side_effect(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "missing-evaluation"

    exit_code = evals_cli_main(
        [
            "run",
            "--suite",
            "ifeval",
            "--dry-run",
            "--case-id",
            "ifeval-json-format",
            "--output",
            str(output),
            "--resume",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.err)["code"] == "evaluation_resume_rejected"
    assert not output.exists()


@pytest.mark.parametrize("budget", (float("nan"), float("inf"), float("-inf")))
def test_custom_request_rejects_non_finite_budget(budget: float) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        parse_evaluation_request(
            _custom_request(
                evaluation_id="eval-non-finite-budget",
                max_wall_seconds=budget,
            )
        )


def test_parse_custom_requires_every_budget_and_selection_field() -> None:
    with pytest.raises(ValueError, match="custom preset requires"):
        parse_evaluation_request(
            {
                "kind": "comparison",
                "bot": "lingye-copilot-qq",
                "profile": "agent-comparison-mvp",
                "preset": "custom",
                "targets": ["native"],
            }
        )


def test_isolated_permission_filter_is_fail_closed() -> None:
    allowed = ToolDef("read_file", "", {}, [], lambda _args: ("", [], None))
    denied = ToolDef("send_message", "", {}, [], lambda _args: ("", [], None))
    check = permission_filter(frozenset({"read_file"}))

    assert check(allowed) is None
    assert check(denied) == "evaluation policy denies this tool"


def test_deterministic_tool_judge_requires_call_and_exact_answer(
    tmp_path: Path,
) -> None:
    case = get_profile("agent-comparison-mvp").cases[2].case

    judge, evidence = judge_profile_trial(
        case,
        "paired-evidence",
        tmp_path,
        [
            {
                "name": "lookup_eval_fact",
                "arguments": {"key": "comparison-token"},
                "ok": True,
            }
        ],
        {},
    )

    assert judge.passed is True
    assert evidence["judge_kind"] == "deterministic:tool-audit"


def test_deterministic_code_judge_runs_fixed_verification_and_captures_diff(
    tmp_path: Path,
) -> None:
    case = get_profile("agent-comparison-mvp").cases[3].case
    before = stage_fixture(case, tmp_path)
    (tmp_path / "calculator.py").write_text(
        "def multiply(left: int, right: int) -> int:\n    return left * right\n",
        encoding="utf-8",
    )

    judge, evidence = judge_profile_trial(case, "fixed", tmp_path, [], before)

    assert judge.passed is True
    assert evidence["returncode"] == 0
    assert "return left * right" in evidence["diff"]
