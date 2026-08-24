from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import shutil
import time
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import chatcopilot.evals.cli as eval_cli_module
import chatcopilot.evals.evaluations as evaluation_module
import chatcopilot.evals.implementation_catalog as implementation_catalog
import chatcopilot.evals.paths as evaluation_paths
import chatcopilot.evals.runner as evaluation_runner
from chatcopilot.agent.tools.registry import ToolMaterializationError
from chatcopilot.botspec.runtime import BotPromptProfile
from chatcopilot.core.config import ChatConfig, LLMConfig, RoutingConfig, RuntimeConfig
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.subagents import SearchProviderSpec
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.evals.cli import main as evals_cli_main
from chatcopilot.evals.artifact_ids import trial_artifact_id
from chatcopilot.evals.capability_executor import CapabilityExecutionError
from chatcopilot.evals.evaluations import (
    EvaluationResult,
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
    _evaluation_tool_provider,
    _isolated_tool_packs,
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
from chatcopilot.evals.report import compare_reports


@pytest.fixture(autouse=True)
def _available_codex_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CHATCOPILOT_LINGYE_API_KEY",
        "test-" + "credential",
    )
    monkeypatch.setattr(
        "chatcopilot.external_tools.codex_cli.command.shutil.which",
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


def _write_repository_markers(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    (root / "src/chatcopilot").mkdir(parents=True)
    return root


def _trial(
    request: TrialExecutionRequest,
    *,
    outcome: str = "passed",
    score: float = 1.0,
) -> EvaluationTrial:
    return EvaluationTrial(
        trial_id=(f"{request.case.case_id}-a{request.attempt}-{request.target.fingerprint[:12]}"),
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
        executor=(request.driver_id or request.target.executor),  # type: ignore[arg-type]
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


def _supervisor_trial_request(tmp_path: Path) -> TrialExecutionRequest:
    output = tmp_path / "supervised-trial"
    output.mkdir()
    request = TrialExecutionRequest(
        evaluation_id="eval-supervised-trial",
        kind="suite",
        bot="",
        output=output,
        suite_id="ifeval",
        profile="",
        profile_case=None,
        case=EvalCase(
            case_id="supervisor-fixture",
            input="fixture",
            category="constraints",
            expected_behavior="fixture",
        ),
        dimension="constraints",
        target=EvaluationTarget(
            target_id="suite",
            label="Suite",
            executor="dry_run",
            backend="suite",
            model="",
            reasoning_effort="",
            fingerprint="f" * 64,
        ),
        attempt=1,
        order=1,
        driver_id="dry_run",
    )
    evaluation_module._reset_trial_workspace(request)
    return request


def _process_tree_probe_executor(request: TrialExecutionRequest) -> EvaluationTrial:
    """Spawn a long-lived grandchild for real supervisor containment tests."""

    marker = Path(str(request.options["marker"]))
    mode = str(request.options["mode"])
    descendant_pid = os.fork()
    if descendant_pid == 0:
        if bool(request.options.get("escape_session", True)):
            os.setsid()
        pending_marker = marker.with_suffix(".pending")
        pending_marker.write_text(str(os.getpid()), encoding="utf-8")
        pending_marker.replace(marker)
        time.sleep(60)
        os._exit(0)
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not marker.exists():
        raise RuntimeError("process-tree probe descendant did not start")
    if mode == "return":
        return _trial(request)
    if mode == "error":
        raise RuntimeError("process-tree probe executor failed")
    if mode == "block":
        time.sleep(60)
        return _trial(request)
    raise ValueError(f"unknown process-tree probe mode {mode!r}")


def _definition_drift_executor(_request: TrialExecutionRequest) -> EvaluationTrial:
    raise evaluation_module._EvaluationDefinitionDrift("fixture definition changed")


def _run_parent_death_probe(request: TrialExecutionRequest) -> None:
    evaluation_module._execute_supervised_trial(
        request,
        budget=evaluation_module._TrialExecutionBudget(seconds=30, scope="case"),
        cancel_check=None,
        executor=_process_tree_probe_executor,
    )


def _assert_linux_process_reaped(pid: int, *, timeout: float = 5.0) -> None:
    process_path = Path(f"/proc/{pid}")
    deadline = time.monotonic() + timeout
    while process_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not process_path.exists(), f"process {pid} remained after Trial cleanup"


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


def _write_managed_comparison_bootstrap(
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
    bot_id = bot_path.parent.name if bot_path.name == "bot.yaml" else bot_path.name
    stored_request = {
        "evaluation_id": parsed.evaluation_id,
        "kind": parsed.kind,
        "bot_id": bot_id,
        "bot_spec": bot_spec,
        "bot_spec_sha256": hashlib.sha256(b"managed-test-bot-spec").hexdigest(),
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
    stored_request["core_request"] = {
        "evaluation_id": parsed.evaluation_id,
        "kind": parsed.kind,
        "bot": bot_spec,
        "profile": parsed.profile,
        "preset": parsed.preset,
        "targets": list(parsed.targets),
        "case_refs": list(parsed.case_refs),
        "repetitions": parsed.repetitions,
        "max_wall_seconds": parsed.max_wall_seconds,
        "seed": parsed.seed,
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
                "pid": os.getpid(),
                "started_at": "2026-07-26T00:00:01+00:00",
                "finished_at": None,
                "duration_seconds": None,
                "completed_trials": 0,
                "planned_trials": (
                    len(parsed.case_refs) * parsed.repetitions * len(parsed.targets)
                ),
                "error": None,
                **(state_overrides or {}),
            }
        ),
        encoding="utf-8",
    )
    (output / "run.log").write_text(run_log, encoding="utf-8")
    if os.name != "nt":
        output.chmod(0o700)
        for artifact in output.iterdir():
            artifact.chmod(0o600)


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

    with pytest.raises(ValueError, match="must be empty"):
        run_evaluation(
            _custom_request(evaluation_id="eval-stale-output"),
            output=output,
            trial_executor=_trial,
        )

    assert stale.read_bytes() == before
    assert list(output.iterdir()) == [stale]


def test_standalone_core_rejects_managed_service_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _write_repository_markers(tmp_path / "repo")
    console = repository / "console"
    console.mkdir()
    monkeypatch.chdir(console)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(repository))
    monkeypatch.delenv("CHATCOPILOT_EVALUATION_ROOT", raising=False)
    output = repository / "reports/evals/evaluations/eval-reserved-core"

    with pytest.raises(ValueError, match="managed service root"):
        run_evaluation(
            {
                "evaluation_id": output.name,
                "kind": "suite",
                "suite": "ifeval",
                "case_ids": ["ifeval-json-format"],
                "dry_run": True,
                "llm_judge": False,
            },
            output=output,
            trial_executor=_trial,
        )

    assert not output.exists()


def test_standalone_core_rejects_configured_managed_service_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "configured-evaluations"
    managed_root.mkdir()
    sentinel = managed_root / "keep.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    monkeypatch.setenv("CHATCOPILOT_EVALUATION_ROOT", str(managed_root))
    output = managed_root / "eval-configured-core"

    with pytest.raises(ValueError, match="managed service root"):
        run_evaluation(
            _custom_request(evaluation_id=output.name),
            output=output,
            trial_executor=_trial,
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert not output.exists()


@pytest.mark.parametrize(
    ("overrides", "run_log", "message"),
    (
        ({"bot_spec": "bots/other/bot.yaml"}, "", "request does not match"),
        ({"bot_id": "other-bot"}, "", "request does not match"),
        ({}, "stale output", "run.log must be empty"),
    ),
)
def test_managed_run_rejects_forged_or_used_service_bootstrap(
    tmp_path: Path,
    overrides: dict,
    run_log: str,
    message: str,
) -> None:
    request = _custom_request(evaluation_id="eval-forged-bootstrap")
    request["bot"] = "bots/lingye-copilot-qq/bot.yaml"
    output = tmp_path / "eval-forged-bootstrap"
    _write_managed_comparison_bootstrap(
        request,
        output,
        request_overrides=overrides,
        run_log=run_log,
    )
    before = {path.name: path.read_bytes() for path in output.iterdir()}

    with pytest.raises(ValueError, match=message):
        run_evaluation(request, output=output, trial_executor=_trial, managed=True)

    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


@pytest.mark.parametrize(
    ("request_overrides", "state_overrides", "message"),
    (
        ({"unexpected": True}, {}, "request does not match"),
        (
            {"start_request_fingerprint": "g" * 64},
            {},
            "start request fingerprint is invalid",
        ),
        (
            {"bot_spec_sha256": "g" * 64},
            {},
            "BotSpec snapshot digest is invalid",
        ),
        ({}, {"unexpected": True}, "state fields"),
        ({}, {"completed_trials": 1}, "state does not match"),
        ({}, {"error": "forged"}, "state does not match"),
        (
            {},
            {"status": "queued", "pid": None, "started_at": None},
            "state does not match",
        ),
        ({}, {"pid": os.getpid() + 100_000}, "state does not match"),
    ),
)
def test_managed_run_requires_exact_service_bootstrap_shape(
    tmp_path: Path,
    request_overrides: dict,
    state_overrides: dict,
    message: str,
) -> None:
    request = _custom_request(evaluation_id="eval-strict-bootstrap")
    request["bot"] = "bots/lingye-copilot-qq/bot.yaml"
    output = tmp_path / "eval-strict-bootstrap"
    _write_managed_comparison_bootstrap(
        request,
        output,
        request_overrides=request_overrides,
        state_overrides=state_overrides,
    )
    before = {path.name: path.read_bytes() for path in output.iterdir()}

    with pytest.raises(ValueError, match=message):
        run_evaluation(request, output=output, trial_executor=_trial, managed=True)

    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX artifact metadata boundary")
@pytest.mark.parametrize("artifact_name", ("request.json", "state.json"))
@pytest.mark.parametrize(
    ("violation", "message"),
    (
        ("owner", "owned by the current user"),
        ("mode", "mode 0600"),
        ("hardlink", "exactly one hard link"),
    ),
)
def test_managed_run_rejects_unsafe_bootstrap_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_name: str,
    violation: str,
    message: str,
) -> None:
    request = _custom_request(evaluation_id="eval-unsafe-bootstrap")
    request["bot"] = "bots/lingye-copilot-qq/bot.yaml"
    output = tmp_path / "eval-unsafe-bootstrap"
    _write_managed_comparison_bootstrap(request, output)
    artifact = output / artifact_name
    original = artifact.read_bytes()

    if violation == "owner":
        owner_uid = artifact.stat().st_uid
        monkeypatch.setattr(evaluation_module.os, "getuid", lambda: owner_uid + 1)
    elif violation == "mode":
        artifact.chmod(0o644)
    else:
        os.link(artifact, tmp_path / f"{artifact_name}.alias")

    with pytest.raises((PermissionError, ValueError), match=message):
        run_evaluation(request, output=output, trial_executor=_trial, managed=True)

    assert artifact.read_bytes() == original
    if violation == "mode":
        assert artifact.stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX artifact metadata boundary")
@pytest.mark.parametrize(
    ("violation", "message"),
    (
        ("owner", "owned by the current user"),
        ("mode", "mode 0600"),
        ("hardlink", "exactly one hard link"),
    ),
)
def test_progress_append_rejects_unsafe_existing_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    violation: str,
    message: str,
) -> None:
    output = tmp_path / "eval-progress-inode"
    output.mkdir(mode=0o700)
    progress = output / "progress.jsonl"
    progress.write_text('{"event":"existing"}\n', encoding="utf-8")
    progress.chmod(0o600)
    original = progress.read_bytes()

    if violation == "owner":
        owner_uid = progress.stat().st_uid
        monkeypatch.setattr(evaluation_module.os, "getuid", lambda: owner_uid + 1)
    elif violation == "mode":
        progress.chmod(0o644)
    else:
        os.link(progress, tmp_path / "progress.alias")

    with pytest.raises((PermissionError, ValueError), match=message):
        evaluation_module._append_jsonl(progress, {"event": "forbidden"})

    assert progress.read_bytes() == original
    if violation == "mode":
        assert progress.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "evaluation_id",
    ("contains.dot", "x" * 129),
)
def test_core_evaluation_id_matches_console_grammar(evaluation_id: str) -> None:
    with pytest.raises(ValueError, match="evaluation_id"):
        parse_evaluation_request(_custom_request(evaluation_id=evaluation_id))


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


def test_codex_preflight_uses_explicit_binary_outside_service_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("CHATCOPILOT_CODEX_BIN", str(codex))
    monkeypatch.setattr(
        "chatcopilot.external_tools.codex_cli.command.shutil.which",
        lambda _binary: None,
    )

    result = validate_evaluation(
        {
            "kind": "suite",
            "bot": "lingye-copilot-qq",
            "suite": "agentstrata-capabilities-v1",
            "preset": "quick",
        }
    )

    executor = next(item for item in result["checks"] if item["code"] == "executor")
    assert result["ready"] is True
    assert executor["ok"] is True
    assert "command=available" in executor["detail"]


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


def test_product_dry_run_validates_static_capability_catalog_before_runtime_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evaluation_module,
        "load_evaluation_runtime",
        lambda _bot: pytest.fail("dry-run must reject invalid definitions before runtime loading"),
    )

    result = validate_evaluation(
        {
            "kind": "suite",
            "suite": "agentstrata-capabilities-v1",
            "preset": "quick",
            "dry_run": True,
        }
    )

    assert result["ready"] is True
    assert (
        next(item for item in result["checks"] if item["code"] == "capability_catalog")["ok"]
        is True
    )


def test_product_dry_run_rejects_invalid_static_verifier_before_runtime_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_definition(_definition: object) -> None:
        raise CapabilityExecutionError(
            "capability_verifier_not_registered", "fixture verifier is not registered"
        )

    monkeypatch.setattr(evaluation_module, "validate_capability_definition", reject_definition)
    monkeypatch.setattr(
        evaluation_module,
        "load_evaluation_runtime",
        lambda _bot: pytest.fail("invalid dry-run must not load runtime"),
    )

    result = validate_evaluation(
        {
            "kind": "suite",
            "suite": "agentstrata-capabilities-v1",
            "preset": "quick",
            "dry_run": True,
        }
    )

    assert result["ready"] is False
    catalog = next(item for item in result["checks"] if item["code"] == "capability_catalog")
    assert catalog["ok"] is False
    assert "capability_verifier_not_registered" in catalog["detail"]
    assert result["targets"] == []


def test_product_dry_run_rejects_case_plugin_driver_drift_before_runtime_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = evaluation_module.load_case_definitions

    def drifted_definitions(manifest: object) -> tuple[object, ...]:
        definitions = original(manifest)  # type: ignore[arg-type]
        return tuple(
            replace(item, driver_id="agent_isolated")
            if item.case_id == "dialogue-strict-json"
            else item
            for item in definitions
        )

    monkeypatch.setattr(evaluation_module, "load_case_definitions", drifted_definitions)
    monkeypatch.setattr(
        evaluation_module,
        "load_evaluation_runtime",
        lambda _bot: pytest.fail("definition drift must not load runtime"),
    )

    result = validate_evaluation(
        {
            "kind": "suite",
            "suite": "agentstrata-capabilities-v1",
            "preset": "quick",
            "dry_run": True,
        }
    )

    assert result["ready"] is False
    catalog = next(item for item in result["checks"] if item["code"] == "capability_catalog")
    assert catalog["ok"] is False
    assert "plugin/driver differs" in catalog["detail"]


def test_named_capability_preset_accepts_console_empty_case_list() -> None:
    parsed = parse_evaluation_request(
        {
            "kind": "suite",
            "bot": "bots/lingye-copilot-qq/bot.yaml",
            "suite": "agentstrata-capabilities-v1",
            "preset": "quick",
            "case_ids": [],
            "dry_run": True,
        }
    )

    assert parsed.kind == "suite"
    assert parsed.preset == "quick"
    assert len(parsed.case_ids) == 10


def _capability_case_preflight(
    case_id: str,
    *,
    providers: tuple[SearchProviderSpec, ...] = (),
    mcp_servers: tuple[McpServerConfig, ...] = (),
    chat_model: str = "commercial-model",
    chat_credential: str = "credential",
) -> list[dict[str, object]]:
    manifest = evaluation_module.get_manifest("agentstrata-capabilities-v1")
    case = next(
        item
        for item in evaluation_module.get_cases(manifest.suite_id, auto_prepare=False)
        if item.case_id == case_id
    )
    runtime = SimpleNamespace(
        agent_backend="codex",
        platform_type="qq",
        tool_features=(),
        memory_namespace="",
        tool_packs=(),
        exclude_tools=(),
        subagents=SimpleNamespace(research_enabled=True, search_providers=providers),
        mcp_servers=mcp_servers,
    )
    config = SimpleNamespace(
        llm=SimpleNamespace(model=chat_model, api_key=chat_credential),
    )
    definitions = evaluation_module._validated_capability_definitions(manifest, (case,))
    return evaluation_module._suite_case_preflight(
        manifest=manifest,
        cases=(case,),
        runtime=runtime,
        config=config,
        capability_definitions=definitions,
    )


def test_qq_persona_preflight_projects_enabled_tool_pack() -> None:
    manifest = evaluation_module.get_manifest("agentstrata-qq-message-flow-v1")
    case = next(
        item
        for item in evaluation_module.get_cases(manifest.suite_id, auto_prepare=False)
        if item.case_id == "qq-persona-persistence-next-turn"
    )
    runtime = SimpleNamespace(
        agent_backend="codex",
        platform_type="qq",
        tool_features=(),
        memory_namespace="",
        tool_packs=("persona.control",),
        exclude_tools=(),
        subagents=SimpleNamespace(
            search_providers=(),
        ),
        mcp_servers=(),
        access=SimpleNamespace(
            enabled=True,
            whitelist_env="QQ_ALLOW_FROM",
            group_whitelist_env="QQ_ALLOW_GROUPS",
            private_require_whitelist=True,
            group_require_whitelist=True,
            group_require_mention=True,
        ),
    )
    definitions = evaluation_module._validated_capability_definitions(manifest, (case,))

    checks = evaluation_module._suite_case_preflight(
        manifest=manifest,
        cases=(case,),
        runtime=runtime,
        config=SimpleNamespace(llm=SimpleNamespace(model="test", api_key="credential")),
        capability_definitions=definitions,
    )

    case_check = next(item for item in checks if item["code"].startswith("case_requirements:"))
    assert case_check["ok"] is True


def test_product_search_preflight_distinguishes_web_from_explicit_experience_source() -> None:
    web_provider = SearchProviderSpec(id="searxng", kind="searxng", enabled=True)

    general = _capability_case_preflight("search-general-with-evidence", providers=(web_provider,))
    general_case = next(item for item in general if item["code"].startswith("case_requirements:"))
    assert general_case["ok"] is True

    explicit = _capability_case_preflight("search-explicit-source", providers=(web_provider,))
    explicit_case = next(item for item in explicit if item["code"].startswith("case_requirements:"))
    assert explicit_case["ok"] is False
    assert "search_source:experience" in str(explicit_case["detail"])
    assert "tool:search_information" in str(explicit_case["detail"])


def test_product_search_preflight_requires_declared_direct_provider_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SearchProviderSpec(
        id="tavily",
        kind="tavily",
        enabled=True,
        credential_env="TEST_EVAL_TAVILY_KEY",
    )

    missing = _capability_case_preflight("search-general-with-evidence", providers=(provider,))
    missing_case = next(item for item in missing if item["code"].startswith("case_requirements:"))
    assert missing_case["ok"] is False
    assert "search_source:web" in str(missing_case["detail"])

    monkeypatch.setenv("TEST_EVAL_TAVILY_KEY", "configured-test-key")
    ready = _capability_case_preflight("search-general-with-evidence", providers=(provider,))
    ready_case = next(item for item in ready if item["code"].startswith("case_requirements:"))
    assert ready_case["ok"] is True


def test_product_search_preflight_accepts_enabled_trusted_experience_mcp() -> None:
    checks = _capability_case_preflight(
        "search-explicit-source",
        mcp_servers=(
            McpServerConfig(
                id="xiaohongshu",
                catalog_ref="xiaohongshu-search",
                enabled=True,
                risk="search",
                search_only_tools=("search_feeds",),
            ),
        ),
    )

    case_check = next(item for item in checks if item["code"].startswith("case_requirements:"))
    assert case_check["ok"] is True


def test_product_subagent_preflight_requires_chat_llm_for_codex_main_backend() -> None:
    missing = _capability_case_preflight(
        "subagent-structured-result", chat_model="", chat_credential=""
    )
    missing_case = next(item for item in missing if item["code"].startswith("case_requirements:"))
    assert missing_case["ok"] is False
    assert "chat_llm_model" in str(missing_case["detail"])
    assert "chat_llm_credential" in str(missing_case["detail"])

    ready = _capability_case_preflight("subagent-structured-result")
    ready_case = next(item for item in ready if item["code"].startswith("case_requirements:"))
    assert ready_case["ok"] is True


def test_product_preflight_reports_enabled_tool_pack_materialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evaluation_module,
        "discover_tools",
        lambda **_kwargs: (_ for _ in ()).throw(
            ToolMaterializationError(
                module="chatcopilot.external_tools.example.tools",
                pack_names=("example.pack",),
                reason="import_error:ImportError",
            )
        ),
    )

    checks = _capability_case_preflight("subagent-structured-result")

    materialization = next(item for item in checks if item["code"] == "tool_materialization")
    assert materialization["ok"] is False
    assert "example.pack" in str(materialization["detail"])


def test_suite_request_rejects_legacy_external_write_authority() -> None:
    with pytest.raises(ValueError, match="Evaluation does not support external writes"):
        parse_evaluation_request(
            {
                "evaluation_id": "eval-no-external-write",
                "kind": "suite",
                "bot": "bots/lingye-copilot-qq/bot.yaml",
                "suite": "agentstrata-capabilities-v1",
                "preset": "quick",
                "confirm_external_write": True,
            }
        )


def test_managed_suite_bootstrap_includes_complete_effective_request(
    tmp_path: Path,
) -> None:
    request = {
        "evaluation_id": "eval-managed-suite",
        "kind": "suite",
        "bot": "bots/lingye-copilot-qq/bot.yaml",
        "suite": "ifeval",
        "preset": "custom",
        "case_ids": ["ifeval-json-format"],
        "repetitions": 2,
        "max_wall_seconds": 90,
        "seed": 7,
        "options": {},
        "confirm_external_write": False,
        "dry_run": True,
        "llm_judge": False,
    }
    parsed = parse_evaluation_request(request)
    validation = validate_evaluation(request)
    targets = tuple(EvaluationTarget(**item) for item in validation["targets"])
    output = tmp_path / "eval-managed-suite"
    output.mkdir(mode=0o700)
    stored_request = {
        **evaluation_module._expected_bootstrap_request(parsed, targets),
        "bot_spec_sha256": hashlib.sha256(b"managed-test-bot-spec").hexdigest(),
        "created_at": "2026-08-17T00:00:00+00:00",
        "core_request": evaluation_module._runnable_request_dict(parsed),
    }
    (output / "request.json").write_text(json.dumps(stored_request), encoding="utf-8")
    (output / "state.json").write_text(
        json.dumps(
            {
                "evaluation_id": parsed.evaluation_id,
                "kind": parsed.kind,
                "status": "running",
                "pid": os.getpid(),
                "started_at": "2026-08-17T00:00:01+00:00",
                "finished_at": None,
                "duration_seconds": None,
                "completed_trials": 0,
                "planned_trials": 2,
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        for artifact in output.iterdir():
            artifact.chmod(0o600)

    result = run_evaluation(
        request,
        output=output,
        trial_executor=_trial,
        managed=True,
    )

    assert result.status == "completed"
    assert len(result.trials) == 2
    assert stored_request["preset"] == "custom"
    assert stored_request["repetitions"] == 2
    assert stored_request["max_wall_seconds"] == 90.0
    assert stored_request["seed"] == 7


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


def test_production_trial_uses_spawn_supervisor(tmp_path: Path) -> None:
    output = tmp_path / "eval-supervised-spawn"

    result = run_evaluation(
        {
            "evaluation_id": "eval-supervised-spawn",
            "kind": "suite",
            "suite": "ifeval",
            "preset": "custom",
            "case_ids": ["ifeval-json-format"],
            "repetitions": 1,
            "max_wall_seconds": 30,
            "seed": 17,
            "dry_run": True,
        },
        output=output,
    )

    assert result.status == "completed"
    assert len(result.trials) == 1
    assert result.trials[0].outcome == "skipped"
    assert result.trials[0].executor == "dry_run"


def test_supervisor_preserves_definition_drift_as_a_fail_closed_signal(tmp_path: Path) -> None:
    request = _supervisor_trial_request(tmp_path)

    with pytest.raises(
        evaluation_module._EvaluationDefinitionDrift,
        match="fixture definition changed",
    ):
        evaluation_module._execute_supervised_trial(
            request,
            budget=evaluation_module._TrialExecutionBudget(seconds=10, scope="case"),
            cancel_check=None,
            executor=_definition_drift_executor,
        )


def test_definition_drift_discards_target_group_without_checkpoint(tmp_path: Path) -> None:
    output = tmp_path / "eval-definition-drift"

    result = run_evaluation(
        {
            "evaluation_id": "eval-definition-drift",
            "kind": "suite",
            "suite": "ifeval",
            "preset": "custom",
            "case_ids": ["ifeval-json-format"],
            "dry_run": True,
        },
        output=output,
        trial_executor=_definition_drift_executor,
    )

    assert result.status == "error"
    assert result.trials == ()
    assert "evaluation definition drift" in result.error
    events = [
        json.loads(line)
        for line in (output / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "evaluation_definition_drift" in {event["event"] for event in events}
    assert not list((output / "trials").glob("*.json"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group supervision")
def test_supervisor_hard_timeout_terminates_active_trial_process(tmp_path: Path) -> None:
    request = _supervisor_trial_request(tmp_path)
    marker = request.output / "workspaces" / request.case.case_id / "worker.pid"
    descendant_marker = marker.with_name("descendant.pid")

    def blocking_executor(trial_request: TrialExecutionRequest) -> EvaluationTrial:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(os.getpid()), encoding="utf-8")
        descendant_pid = os.fork()
        if descendant_pid == 0:
            descendant_marker.write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(60)
            os._exit(0)
        while not descendant_marker.exists():
            time.sleep(0.01)
        time.sleep(60)
        return _trial(trial_request)

    with pytest.raises(
        evaluation_module._TrialExecutionDeadlineExceeded,
        match="case execution deadline exceeded",
    ):
        evaluation_module._execute_supervised_trial(
            request,
            budget=evaluation_module._TrialExecutionBudget(seconds=0.25, scope="case"),
            cancel_check=None,
            executor=blocking_executor,
            _context=multiprocessing.get_context("fork"),
        )

    worker_pid = int(marker.read_text(encoding="utf-8"))
    descendant_pid = int(descendant_marker.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)
    descendant_state_path = Path(f"/proc/{descendant_pid}/stat")
    deadline = time.monotonic() + 2
    while descendant_state_path.exists() and time.monotonic() < deadline:
        state = descendant_state_path.read_text(encoding="utf-8").split()[2]
        if state == "Z":
            break
        time.sleep(0.02)
    if descendant_state_path.exists():
        assert descendant_state_path.read_text(encoding="utf-8").split()[2] == "Z"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group supervision")
def test_supervisor_cancel_terminates_in_flight_trial(tmp_path: Path) -> None:
    request = _supervisor_trial_request(tmp_path)
    marker = request.output / "workspaces" / request.case.case_id / "cancel.pid"

    def blocking_executor(trial_request: TrialExecutionRequest) -> EvaluationTrial:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(60)
        return _trial(trial_request)

    with pytest.raises(
        evaluation_module._TrialExecutionCancelled,
        match="cancelled during an active Trial",
    ):
        evaluation_module._execute_supervised_trial(
            request,
            budget=evaluation_module._TrialExecutionBudget(seconds=10, scope="case"),
            cancel_check=marker.exists,
            executor=blocking_executor,
            _context=multiprocessing.get_context("fork"),
        )

    worker_pid = int(marker.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group supervision")
def test_supervisor_waits_for_cleanup_ready_before_immediate_cancel(tmp_path: Path) -> None:
    request = _supervisor_trial_request(tmp_path)

    with pytest.raises(
        evaluation_module._TrialExecutionCancelled,
        match="cancelled during an active Trial",
    ):
        evaluation_module._execute_supervised_trial(
            request,
            budget=evaluation_module._TrialExecutionBudget(seconds=10, scope="case"),
            cancel_check=lambda: True,
            executor=_trial,
            _context=multiprocessing.get_context("fork"),
        )


@pytest.mark.skipif(
    not os.path.isdir("/proc/self/task"),
    reason="Linux subreaper supervision requires /proc",
)
@pytest.mark.parametrize("mode", ("return", "error"))
def test_spawn_supervisor_reaps_setsid_descendant_after_terminal_frame(
    tmp_path: Path,
    mode: str,
) -> None:
    request = _supervisor_trial_request(tmp_path)
    marker = request.output / f"{mode}-escaped-descendant.pid"
    request = replace(
        request,
        options={"marker": str(marker), "mode": mode, "escape_session": True},
    )

    if mode == "error":
        with pytest.raises(RuntimeError, match="process-tree probe executor failed"):
            evaluation_module._execute_supervised_trial(
                request,
                budget=evaluation_module._TrialExecutionBudget(seconds=10, scope="case"),
                cancel_check=None,
                executor=_process_tree_probe_executor,
            )
    else:
        trial = evaluation_module._execute_supervised_trial(
            request,
            budget=evaluation_module._TrialExecutionBudget(seconds=10, scope="case"),
            cancel_check=None,
            executor=_process_tree_probe_executor,
        )
        assert trial.outcome == "passed"

    _assert_linux_process_reaped(int(marker.read_text(encoding="utf-8")))


@pytest.mark.skipif(
    not os.path.isdir("/proc/self/task"),
    reason="Linux subreaper supervision requires /proc",
)
def test_spawn_supervisor_timeout_reaps_setsid_descendant(tmp_path: Path) -> None:
    request = _supervisor_trial_request(tmp_path)
    marker = request.output / "timeout-escaped-descendant.pid"
    request = replace(
        request,
        options={"marker": str(marker), "mode": "block", "escape_session": True},
    )

    with pytest.raises(evaluation_module._TrialExecutionDeadlineExceeded):
        evaluation_module._execute_supervised_trial(
            request,
            budget=evaluation_module._TrialExecutionBudget(seconds=1.5, scope="case"),
            cancel_check=None,
            executor=_process_tree_probe_executor,
        )

    _assert_linux_process_reaped(int(marker.read_text(encoding="utf-8")))


@pytest.mark.skipif(
    not os.path.isdir("/proc/self/task"),
    reason="Linux subreaper supervision requires /proc",
)
def test_spawn_supervisor_cancel_reaps_setsid_descendant(tmp_path: Path) -> None:
    request = _supervisor_trial_request(tmp_path)
    marker = request.output / "cancel-escaped-descendant.pid"
    request = replace(
        request,
        options={"marker": str(marker), "mode": "block", "escape_session": True},
    )

    with pytest.raises(evaluation_module._TrialExecutionCancelled):
        evaluation_module._execute_supervised_trial(
            request,
            budget=evaluation_module._TrialExecutionBudget(seconds=10, scope="case"),
            cancel_check=marker.exists,
            executor=_process_tree_probe_executor,
        )

    _assert_linux_process_reaped(int(marker.read_text(encoding="utf-8")))


@pytest.mark.skipif(
    not os.path.isdir("/proc/self/task"),
    reason="Linux subreaper supervision requires /proc",
)
def test_trial_supervisor_parent_death_reaps_setsid_descendant(tmp_path: Path) -> None:
    request = _supervisor_trial_request(tmp_path)
    marker = request.output / "parent-death-escaped-descendant.pid"
    request = replace(
        request,
        options={"marker": str(marker), "mode": "block", "escape_session": True},
    )
    context = multiprocessing.get_context("fork")
    parent = context.Process(target=_run_parent_death_probe, args=(request,), daemon=False)
    parent.start()
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "parent-death probe descendant did not start"
        descendant_pid = int(marker.read_text(encoding="utf-8"))
        assert parent.pid is not None
        child_values = Path(f"/proc/{parent.pid}/task/{parent.pid}/children").read_text(
            encoding="ascii"
        )
        supervisor_pids = [
            int(value)
            for value in child_values.split()
            if b"spawn_main" in Path(f"/proc/{value}/cmdline").read_bytes()
        ]
        assert len(supervisor_pids) == 1
        os.kill(parent.pid, signal.SIGKILL)
        parent.join(timeout=5)
        assert not parent.is_alive()
        _assert_linux_process_reaped(supervisor_pids[0])
        _assert_linux_process_reaped(descendant_pid)
    finally:
        if parent.is_alive():
            parent.kill()
            parent.join(timeout=5)
        parent.close()


def test_trial_ipc_uses_bounded_canonical_json_bytes_only() -> None:
    class SendBytesOnly:
        def __init__(self) -> None:
            self.encoded = b""

        def send_bytes(self, encoded: bytes) -> None:
            self.encoded = encoded

        def send(self, _payload: object) -> None:
            raise AssertionError("pickle send must not be used")

    sender = SendBytesOnly()
    evaluation_module._send_trial_ipc_frame(sender, {"pid": 7, "kind": "ready"})
    assert sender.encoded == b'{"kind":"ready","pid":7}'

    class ReceiveBytesOnly:
        def recv_bytes(self, maxlength: int) -> bytes:
            assert maxlength == evaluation_module._MAX_TRIAL_IPC_FRAME_BYTES
            return sender.encoded

        def recv(self) -> object:
            raise AssertionError("pickle recv must not be used")

    assert evaluation_module._recv_trial_ipc_frame(ReceiveBytesOnly()) == {
        "kind": "ready",
        "pid": 7,
    }
    with pytest.raises(ValueError, match="exceeds"):
        evaluation_module._encode_trial_ipc_frame(
            {"blob": "x" * evaluation_module._MAX_TRIAL_IPC_FRAME_BYTES}
        )

    class RawFrame:
        def __init__(self, encoded: bytes) -> None:
            self.encoded = encoded

        def recv_bytes(self, maxlength: int) -> bytes:
            assert maxlength == evaluation_module._MAX_TRIAL_IPC_FRAME_BYTES
            return self.encoded

    with pytest.raises(ValueError, match="canonical"):
        evaluation_module._recv_trial_ipc_frame(RawFrame(b'{"pid":7, "kind":"ready"}'))
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        evaluation_module._recv_trial_ipc_frame(RawFrame(b'{"kind":NaN}'))
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        evaluation_module._recv_trial_ipc_frame(RawFrame(b'{"kind":"ready","kind":"ready"}'))


def test_unproven_supervisor_cleanup_is_fatal_and_process_is_not_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _supervisor_trial_request(tmp_path)

    class FakeConnection:
        def close(self) -> None:
            pass

        def poll(self, timeout: float) -> bool:
            time.sleep(timeout)
            return False

    class StuckProcess:
        pid = 987654
        exitcode = None

        def __init__(self) -> None:
            self.close_calls = 0

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float = 0) -> None:
            del timeout

        def terminate(self) -> None:
            pass

        def close(self) -> None:
            self.close_calls += 1

    process = StuckProcess()

    class FakeContext:
        def Pipe(self, *, duplex: bool) -> tuple[FakeConnection, FakeConnection]:
            assert duplex is False
            return FakeConnection(), FakeConnection()

        def Process(self, **_kwargs: object) -> StuckProcess:
            return process

    monkeypatch.setattr(evaluation_module.os, "killpg", lambda _pid, _signal: None)
    monkeypatch.setattr(evaluation_module, "_TRIAL_TERMINATE_GRACE_SECONDS", 0.01)

    with pytest.raises(evaluation_module._TrialCleanupFailed, match="did not finish"):
        evaluation_module._execute_supervised_trial(
            request,
            budget=evaluation_module._TrialExecutionBudget(seconds=10, scope="case"),
            cancel_check=lambda: True,
            _context=FakeContext(),
        )

    assert process.close_calls == 0


def test_in_flight_evaluation_budget_discards_uncheckpointed_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "eval-supervised-wall-timeout"

    def deadline(*_args: object, **_kwargs: object) -> EvaluationTrial:
        raise evaluation_module._TrialExecutionDeadlineExceeded(
            scope="evaluation",
            seconds=0.25,
        )

    monkeypatch.setattr(evaluation_module, "_execute_supervised_trial", deadline)
    result = run_evaluation(
        {
            "evaluation_id": "eval-supervised-wall-timeout",
            "kind": "suite",
            "suite": "ifeval",
            "preset": "custom",
            "case_ids": ["ifeval-json-format"],
            "repetitions": 1,
            "max_wall_seconds": 30,
            "seed": 17,
            "dry_run": True,
        },
        output=output,
    )

    assert result.status == "partial"
    assert result.trials == ()
    assert not any((output / "workspaces").iterdir())
    progress = (output / "progress.jsonl").read_text(encoding="utf-8")
    assert '"event":"evaluation_budget_exhausted"' in progress
    assert '"event":"target_group_discarded"' in progress


def test_in_flight_case_timeout_is_an_infrastructure_error_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "eval-supervised-case-timeout"

    def deadline(*_args: object, **_kwargs: object) -> EvaluationTrial:
        raise evaluation_module._TrialExecutionDeadlineExceeded(
            scope="case",
            seconds=0.25,
        )

    monkeypatch.setattr(evaluation_module, "_execute_supervised_trial", deadline)
    result = run_evaluation(
        {
            "evaluation_id": "eval-supervised-case-timeout",
            "kind": "suite",
            "suite": "ifeval",
            "preset": "custom",
            "case_ids": ["ifeval-json-format"],
            "repetitions": 1,
            "max_wall_seconds": 30,
            "seed": 17,
            "dry_run": True,
        },
        output=output,
    )

    assert result.status == "completed"
    assert len(result.trials) == 1
    assert result.trials[0].outcome == "error"
    assert "case execution deadline exceeded" in result.trials[0].error


def test_cleanup_failure_is_fatal_quarantines_workspace_and_rejects_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "eval-supervisor-cleanup-failed"
    calls = 0

    def cleanup_failed(*_args: object, **_kwargs: object) -> EvaluationTrial:
        nonlocal calls
        calls += 1
        raise evaluation_module._TrialCleanupFailed("descendant cleanup was not proven")

    monkeypatch.setattr(evaluation_module, "_execute_supervised_trial", cleanup_failed)
    request = {
        "evaluation_id": "eval-supervisor-cleanup-failed",
        "kind": "suite",
        "suite": "ifeval",
        "preset": "custom",
        "case_ids": ["ifeval-json-format"],
        "repetitions": 2,
        "max_wall_seconds": 30,
        "seed": 17,
        "dry_run": True,
    }

    result = run_evaluation(request, output=output)

    assert result.status == "error"
    assert result.trials == ()
    assert result.error.startswith(evaluation_module._TRIAL_CLEANUP_ERROR_PREFIX)
    assert calls == 1
    assert any((output / "workspaces").iterdir())
    progress = (output / "progress.jsonl").read_text(encoding="utf-8")
    assert '"event":"trial_cleanup_failed"' in progress
    assert '"event":"target_group_quarantined"' in progress
    assert '"event":"target_group_discarded"' not in progress

    with pytest.raises(ValueError, match="quarantined Evaluation"):
        run_evaluation(request, output=output, resume=True)


def test_trial_authority_mutation_is_indeterminate_and_quarantined(
    tmp_path: Path,
) -> None:
    output = tmp_path / "eval-artifact-integrity-violation"
    request = _custom_request(
        evaluation_id="eval-artifact-integrity-violation",
        targets=["native"],
    )

    def mutate_authority(trial_request: TrialExecutionRequest) -> EvaluationTrial:
        (trial_request.output / "request.json").write_text(
            '{"tampered":true}\n',
            encoding="utf-8",
        )
        return _trial(trial_request)

    result = run_evaluation(
        request,
        output=output,
        trial_executor=mutate_authority,
    )

    assert result.status == "error"
    assert result.trials == ()
    assert result.error.startswith(evaluation_module._ARTIFACT_INTEGRITY_ERROR_PREFIX)
    assert any((output / "workspaces").iterdir())
    assert '"event":"target_group_quarantined"' in (output / "progress.jsonl").read_text(
        encoding="utf-8"
    )

    with pytest.raises(ValueError, match="quarantined Evaluation"):
        run_evaluation(request, output=output, resume=True)


def test_core_converts_oversized_trial_evidence_to_indeterminate_error(
    tmp_path: Path,
) -> None:
    output = tmp_path / "eval-oversized-evidence"

    def execute(request: TrialExecutionRequest) -> EvaluationTrial:
        return replace(
            _trial(request),
            evidence={"blob": "x" * (evaluation_module._MAX_TRIAL_STRING_CHARS + 1)},
        )

    result = run_evaluation(
        _custom_request(
            evaluation_id="eval-oversized-evidence",
            targets=["native"],
        ),
        output=output,
        trial_executor=execute,
    )

    assert result.status == "completed"
    assert len(result.trials) == 1
    assert result.trials[0].outcome == "error"
    assert result.trials[0].passed is False
    assert "Core integrity limits" in result.trials[0].error


def test_core_rejects_recursive_trial_evidence_before_redaction(tmp_path: Path) -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    output = tmp_path / "eval-recursive-evidence"

    def execute(request: TrialExecutionRequest) -> EvaluationTrial:
        return replace(_trial(request), evidence=recursive)

    result = run_evaluation(
        _custom_request(
            evaluation_id="eval-recursive-evidence",
            targets=["native"],
        ),
        output=output,
        trial_executor=execute,
    )

    assert result.trials[0].outcome == "error"
    assert "recursive mapping" in result.trials[0].error


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


def test_suite_resume_accepts_checkpointed_attempts_above_one(tmp_path: Path) -> None:
    request = {
        "evaluation_id": "eval-capability-repetitions",
        "kind": "suite",
        "suite": "agentstrata-capabilities-v1",
        "preset": "custom",
        "case_ids": ["dialogue-strict-json"],
        "repetitions": 3,
        "max_wall_seconds": 0,
        "seed": 17,
        "dry_run": True,
    }
    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks > 2

    output = tmp_path / "eval-capability-repetitions"
    partial = run_evaluation(
        request,
        output=output,
        cancel_check=cancel,
        trial_executor=_trial,
    )

    assert partial.status == "cancelled"
    assert [trial.attempt for trial in partial.trials] == [1, 2]

    resumed = run_evaluation(
        request,
        output=output,
        resume=True,
        trial_executor=_trial,
    )

    assert resumed.status == "completed"
    assert [trial.attempt for trial in resumed.trials] == [1, 2, 3]


def test_suite_resume_validates_the_case_driver_not_the_mixed_target_driver() -> None:
    request = parse_evaluation_request(
        {
            "evaluation_id": "eval-mixed-driver-resume",
            "kind": "suite",
            "bot": "bots/lingye-copilot-qq/bot.yaml",
            "suite": "agentstrata-capabilities-v1",
            "preset": "custom",
            "case_ids": [
                "dialogue-strict-json",
                "tool-allowed-exact-call",
            ],
            "repetitions": 2,
        }
    )
    assert request.kind == "suite"
    cases = evaluation_module._execution_cases(request)
    isolated_case = next(case for case in cases if case.case_id == "tool-allowed-exact-call")
    target = EvaluationTarget(
        target_id="codex-configured",
        label="Codex configured",
        executor="agent_configured",
        backend="codex",
        model="configured-model",
        reasoning_effort="medium",
        fingerprint="a" * 64,
    )
    execution = evaluation_module._trial_request(
        request=request,
        output=Path("reports/evals/manual/eval-mixed-driver-resume"),
        case=isolated_case,
        target=target,
        attempt=1,
        order=1,
        config_snapshot={
            "definition_snapshot": {"schema": "test-frozen-definition"},
            "definition_fingerprint": "b" * 64,
            "environment_fingerprint": "c" * 64,
        },
    )
    trial = replace(
        _trial(execution),
        trial_id=trial_artifact_id(
            isolated_case.case_id,
            attempt=1,
            target_fingerprint=target.fingerprint,
        ),
    )

    resumed = evaluation_module._validated_resume_trials(
        [evaluation_module.to_jsonable(trial)],
        request=request,
        targets=(target,),
        cases=cases,
    )

    assert resumed[0].executor == "agent_isolated"


def test_suite_resume_rejects_plugin_definition_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "evaluation_id": "eval-capability-definition-drift",
        "kind": "suite",
        "suite": "agentstrata-capabilities-v1",
        "preset": "custom",
        "case_ids": ["dialogue-strict-json"],
        "repetitions": 2,
        "dry_run": True,
    }
    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    output = tmp_path / "eval-capability-definition-drift"
    partial = run_evaluation(
        request,
        output=output,
        cancel_check=cancel,
        trial_executor=_trial,
    )
    assert partial.status == "cancelled"
    original = evaluation_module.suite_definition_snapshot

    def drifted_snapshot(*args: object, **kwargs: object) -> dict:
        snapshot = original(*args, **kwargs)
        return {**snapshot, "test_plugin_drift": True}

    monkeypatch.setattr(
        evaluation_module,
        "suite_definition_snapshot",
        drifted_snapshot,
    )

    with pytest.raises(ValueError, match="definition_fingerprint"):
        run_evaluation(
            request,
            output=output,
            resume=True,
            trial_executor=_trial,
        )


def test_comparison_resume_rejects_execution_implementation_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _custom_request(
        evaluation_id="eval-comparison-implementation-drift",
        repetitions=2,
    )
    output = tmp_path / "eval-comparison-implementation-drift"
    _create_resumable_evaluation(request, output)
    original = evaluation_module.comparison_implementation_snapshot

    def drifted_snapshot() -> dict[str, object]:
        snapshot = original()
        modules = dict(snapshot["modules"])  # type: ignore[arg-type]
        modules["chatcopilot.evals.isolated_executor"] = "f" * 64
        return {**snapshot, "modules": modules}

    monkeypatch.setattr(
        evaluation_module,
        "comparison_implementation_snapshot",
        drifted_snapshot,
    )

    with pytest.raises(ValueError, match="case_hash"):
        run_evaluation(
            request,
            output=output,
            resume=True,
            trial_executor=_trial,
        )


def test_comparison_report_rejects_execution_implementation_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_output = tmp_path / "eval-comparison-implementation-base"
    new_output = tmp_path / "eval-comparison-implementation-new"
    run_evaluation(
        _custom_request(evaluation_id="eval-comparison-implementation-base"),
        output=base_output,
        trial_executor=_trial,
    )
    original = evaluation_module.comparison_implementation_snapshot

    def drifted_snapshot() -> dict[str, object]:
        snapshot = original()
        modules = dict(snapshot["modules"])  # type: ignore[arg-type]
        modules["chatcopilot.evals.profiles"] = "e" * 64
        return {**snapshot, "modules": modules}

    monkeypatch.setattr(
        evaluation_module,
        "comparison_implementation_snapshot",
        drifted_snapshot,
    )
    run_evaluation(
        _custom_request(evaluation_id="eval-comparison-implementation-new"),
        output=new_output,
        trial_executor=_trial,
    )

    with pytest.raises(ValueError, match="case_hash"):
        compare_reports(base_output, new_output)


def test_private_runtime_fingerprint_binds_group_allowlist_without_persisting_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_env = "TEST_EVAL_USER_ALLOWLIST"
    group_env = "TEST_EVAL_GROUP_ALLOWLIST"
    runtime = SimpleNamespace(
        access=SimpleNamespace(
            enabled=True,
            whitelist_env=user_env,
            group_whitelist_env=group_env,
        ),
        spec=SimpleNamespace(llm=SimpleNamespace(env_prefix="TEST_EVAL_GROUP")),
    )
    config = SimpleNamespace(llm=SimpleNamespace(api_key="fallback-eval-key-123456"))
    monkeypatch.setattr(evaluation_module, "load_evaluation_runtime", lambda _bot: runtime)
    monkeypatch.setattr(evaluation_module, "load_config", lambda **_kwargs: config)
    monkeypatch.setenv(user_env, "user-private-17")
    monkeypatch.setenv(group_env, "group-private-23")

    first = evaluation_module._private_runtime_configuration_snapshot("configured-bot")
    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)

    assert first["group_whitelist_configured"] is True
    assert first["group_whitelist_entry_count"] == 1
    assert "user-private-17" not in serialized
    assert "group-private-23" not in serialized

    monkeypatch.setenv(group_env, "group-private-24")
    drifted = evaluation_module._private_runtime_configuration_snapshot("configured-bot")
    assert first["identity_hmac"] != drifted["identity_hmac"]


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
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }

    with pytest.raises(ValueError, match="request_hash"):
        run_evaluation(
            {**request, "seed": request["seed"] + 1},
            output=output,
            trial_executor=_trial,
            resume=True,
        )

    after = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
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
            prompt_profile=replace(
                runtime.prompt_profile,
                identity=runtime.prompt_profile.identity + "\nchanged evaluation behavior",
            ),
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


def test_resolved_chat_config_snapshot_is_complete_and_excludes_secrets() -> None:
    base_url = (
        "https://"
        + "private-user"
        + ":"
        + "private-password"
        + "@example.invalid/v1"
        + "?"
        + "token=secret"
    )
    api_key = "private-api-key-value"
    config = ChatConfig(
        llm=LLMConfig(
            base_url=base_url,
            model="commercial-model",
            api_key=api_key,
            timeout=73,
        ),
        runtime=RuntimeConfig(
            max_tool_iterations=11,
            hard_iteration_cap=31,
            max_tool_calls=17,
            turn_timeout_seconds=101,
            hard_timeout_seconds=202,
            topic_classifier_enabled=True,
            topic_classifier_mode="llm",
        ),
        routing=RoutingConfig(
            code_command=(
                f"/opt/private/codex exec --token {api_key} --model {{model}} --cd {{workdir}}"
            ),
            code_timeout_seconds=321,
        ),
    )

    snapshot = evaluation_module._resolved_chat_config_snapshot(config)
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)

    assert set(snapshot["llm"]) == {"base_url_sha256", "model", "timeout"}
    assert (
        snapshot["llm"]["base_url_sha256"] == hashlib.sha256(base_url.encode("utf-8")).hexdigest()
    )
    assert set(snapshot["runtime"]) == {field.name for field in fields(RuntimeConfig)}
    assert set(snapshot["routing"]) == (
        {field.name for field in fields(RoutingConfig)} - {"code_command"}
    ) | {"code_command_sha256"}
    assert snapshot["runtime"]["max_tool_iterations"] == 11
    assert snapshot["runtime"]["hard_timeout_seconds"] == 202
    assert snapshot["runtime"]["topic_classifier_mode"] == "llm"
    assert (
        snapshot["routing"]["code_command_sha256"]
        == hashlib.sha256(config.routing.code_command.encode("utf-8")).hexdigest()
    )
    assert snapshot["routing"]["code_timeout_seconds"] == 321
    assert base_url not in serialized
    assert api_key not in serialized


def test_target_runtime_fingerprint_covers_resolved_chat_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        source_path=tmp_path / "bots" / "eval-bot" / "bot.yaml",
        spec=SimpleNamespace(raw={}, context={}),
        mcp_servers=(),
        rag_sources=(),
        skills=(),
        prompt_profile=BotPromptProfile(
            identity="system",
            response_style="concise",
            refusal_style="refusal",
        ),
        capability_policies=(),
        tool_packs=(),
        tool_features=(),
        exclude_tools=(),
        agent_backend="native",
        subagents={},
        memory_namespace="",
        access={},
    )
    config = ChatConfig(
        llm=LLMConfig(
            base_url="https://api.example.invalid/v1",
            model="commercial-model",
            api_key="credential-a",
            timeout=120,
        )
    )
    baseline = evaluation_module._runtime_behavior_fingerprint(runtime, config)
    credential_rotated = replace(
        config,
        llm=replace(config.llm, api_key="credential-b"),
    )
    assert evaluation_module._runtime_behavior_fingerprint(runtime, credential_rotated) == baseline

    drifted = (
        replace(config, llm=replace(config.llm, base_url="https://other.invalid/v1")),
        replace(config, llm=replace(config.llm, timeout=121)),
        replace(config, runtime=replace(config.runtime, max_tool_iterations=9)),
        replace(config, runtime=replace(config.runtime, hard_iteration_cap=29)),
        replace(config, runtime=replace(config.runtime, max_tool_calls=7)),
        replace(config, runtime=replace(config.runtime, turn_timeout_seconds=90)),
        replace(config, runtime=replace(config.runtime, hard_timeout_seconds=180)),
        replace(config, runtime=replace(config.runtime, topic_classifier_enabled=True)),
        replace(config, routing=replace(config.routing, code_prefixes=("/different",))),
        replace(config, routing=replace(config.routing, code_command="codex exec --json")),
        replace(config, routing=replace(config.routing, code_timeout_seconds=901)),
    )
    assert all(
        evaluation_module._runtime_behavior_fingerprint(runtime, candidate) != baseline
        for candidate in drifted
    )

    original_snapshot = evaluation_module.runtime_implementation_snapshot
    monkeypatch.setattr(
        evaluation_module,
        "runtime_implementation_snapshot",
        lambda backend: {
            **original_snapshot(backend),
            "modules": {"chatcopilot.agent.runtime": "0" * 64},
        },
    )
    assert evaluation_module._runtime_behavior_fingerprint(runtime, config) != baseline


def _frozen_ifeval_trial_request(tmp_path: Path) -> TrialExecutionRequest:
    parsed = parse_evaluation_request(
        {
            "evaluation_id": "eval-frozen-suite-identity",
            "kind": "suite",
            "suite": "ifeval",
            "preset": "custom",
            "case_ids": ["ifeval-json-format"],
            "dry_run": True,
        }
    )
    validation = validate_evaluation(parsed)
    assert validation["ready"] is True
    targets = tuple(evaluation_module._target_from_dict(item) for item in validation["targets"])
    cases = evaluation_module._execution_cases(parsed)
    snapshot = evaluation_module._config_snapshot(parsed, targets, cases)
    return evaluation_module._trial_request(
        request=parsed,
        output=tmp_path / "frozen-suite",
        case=cases[0],
        target=targets[0],
        attempt=1,
        order=1,
        config_snapshot=snapshot,
    )


def test_suite_trial_revalidates_parent_frozen_case_and_complete_identity(
    tmp_path: Path,
) -> None:
    request = _frozen_ifeval_trial_request(tmp_path)

    evaluation_module._assert_suite_trial_definition_current(request)

    with pytest.raises(
        evaluation_module._EvaluationDefinitionDrift,
        match="parent-frozen EvalCase",
    ):
        evaluation_module._assert_suite_trial_definition_current(
            replace(request, case=replace(request.case, input="drifted input"))
        )


def test_suite_trial_rejects_trusted_source_drift_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _frozen_ifeval_trial_request(tmp_path)
    original_digest = implementation_catalog.trusted_module_sha256
    monkeypatch.setattr(
        implementation_catalog,
        "trusted_module_sha256",
        lambda module_name: (
            "0" * 64 if module_name == "chatcopilot.evals.runner" else original_digest(module_name)
        ),
    )

    with pytest.raises(
        evaluation_module._EvaluationDefinitionDrift,
        match="trusted implementation changed",
    ):
        evaluation_module._assert_suite_trial_definition_current(request)


def test_suite_trial_rejects_environment_drift_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _frozen_ifeval_trial_request(tmp_path)
    monkeypatch.setattr(
        evaluation_module,
        "_private_runtime_configuration_snapshot",
        lambda _bot: {"drift": True},
    )

    with pytest.raises(
        evaluation_module._EvaluationDefinitionDrift,
        match="private runtime environment changed",
    ):
        evaluation_module._assert_suite_trial_definition_current(request)


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
    _write_managed_comparison_bootstrap(request, output)

    def execute(request: TrialExecutionRequest) -> EvaluationTrial:
        return replace(
            _trial(request),
            final_text=f"super-secret-value at {request.output}",
            events=({"type": "ToolFinished", "summary": "api_key=super-secret-value"},),
        )

    run_evaluation(
        request,
        output=output,
        trial_executor=execute,
        managed=True,
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
    identity_checks: list[str] = []

    def fake_run_suite(
        _suite_id: str,
        **kwargs: object,
    ) -> EvalRunResult:
        workspace_roots.append(Path(kwargs["workspace_root"]))  # type: ignore[arg-type]
        assert kwargs["_frozen_cases"] == (case,)
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
    monkeypatch.setattr(
        evaluation_module,
        "_assert_suite_trial_definition_current",
        lambda request: identity_checks.append(request.case.case_id),
    )
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
    assert identity_checks == [case.case_id, case.case_id]


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
    monkeypatch.setattr(
        evaluation_module,
        "_assert_suite_trial_definition_current",
        lambda _request: None,
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


def test_product_suite_summary_never_reports_a_partial_green_or_total_score() -> None:
    cases = evaluation_module._execution_cases(
        parse_evaluation_request(
            {
                "kind": "suite",
                "suite": "agentstrata-capabilities-v1",
                "preset": "custom",
                "case_ids": [
                    "dialogue-strict-json",
                    "tool-allowed-exact-call",
                ],
                "dry_run": True,
            }
        )
    )
    target = EvaluationTarget(
        target_id="dry-run",
        label="Dry Run",
        executor="dry_run",
        backend="none",
        model="",
        reasoning_effort="",
        fingerprint="b" * 64,
    )
    first_execution = TrialExecutionRequest(
        evaluation_id="eval-product-summary",
        kind="suite",
        bot="",
        output=Path("reports/evals/manual/eval-product-summary"),
        suite_id="agentstrata-capabilities-v1",
        profile="",
        profile_case=None,
        case=cases[0],
        dimension=cases[0].category,
        target=target,
        attempt=1,
        order=1,
        driver_id="dry_run",
        dry_run=True,
    )
    trial = _trial(first_execution)

    running = evaluation_module._suite_summary(
        (trial,),
        cases=cases,
        lifecycle_status="running",
        product_suite=True,
        repetitions=1,
    )
    completed = evaluation_module._suite_summary(
        (trial,),
        cases=cases[:1],
        lifecycle_status="completed",
        product_suite=True,
        repetitions=1,
    )

    assert running["verdict"] == "in_progress"
    assert completed["verdict"] == "passed"
    assert "score" not in completed
    assert "max_score" not in completed
    assert "score_ratio" not in completed
    assert completed["capabilities"][cases[0].category] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
    }


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("passed", 0),
        ("failed", 1),
        ("error/indeterminate", 1),
    ],
)
def test_cli_product_suite_exit_code_follows_capability_verdict(
    verdict: str,
    expected: int,
) -> None:
    from chatcopilot.evals.cli import _result_exit_code

    result = EvaluationResult(
        evaluation_id="eval-product-cli",
        kind="suite",
        bot="example-bot",
        status="completed",
        started_at="2026-08-17T00:00:00+00:00",
        finished_at="2026-08-17T00:00:01+00:00",
        duration_seconds=1.0,
        suite="agentstrata-capabilities-v1",
        summary={"verdict": verdict},
    )

    assert _result_exit_code(result) == expected


def test_cli_product_suite_text_has_verdict_and_no_aggregate_score() -> None:
    from chatcopilot.evals.cli import _result_summary_line

    result = EvaluationResult(
        evaluation_id="eval-product-cli",
        kind="suite",
        bot="example-bot",
        status="completed",
        started_at="2026-08-17T00:00:00+00:00",
        finished_at="2026-08-17T00:00:01+00:00",
        duration_seconds=1.0,
        suite="agentstrata-capabilities-v1",
        summary={
            "verdict": "failed",
            "passed": 8,
            "failed": 2,
            "critical_violations": 1,
            "infrastructure_errors": 0,
        },
    )

    line = _result_summary_line(result)

    assert "verdict=failed" in line
    assert "critical_violations=1" in line
    assert "infrastructure_errors=0" in line
    assert "score" not in line


def test_cli_cancelled_is_nonzero_but_official_completed_behavior_is_preserved() -> None:
    from chatcopilot.evals.cli import _result_exit_code, _result_summary_line

    official = EvaluationResult(
        evaluation_id="eval-official-cli",
        kind="suite",
        bot="example-bot",
        status="completed",
        started_at="2026-08-17T00:00:00+00:00",
        finished_at="2026-08-17T00:00:01+00:00",
        duration_seconds=1.0,
        suite="ifeval",
        summary={"score_ratio": 0.25},
    )

    assert _result_exit_code(official) == 0
    assert "score_ratio=0.250" in _result_summary_line(official)
    assert _result_exit_code(replace(official, status="cancelled")) == 130


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


def test_cli_freezes_bot_environment_before_validation_and_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _write_repository_markers(tmp_path / "repo")
    bot_dir = repository / "bots/example"
    bot_dir.mkdir(parents=True)
    (bot_dir / "bot.yaml").write_text(
        "id: example\n"
        "llm:\n"
        "  chat:\n"
        "    env_prefix: CHATCOPILOT_TESTBOT\n"
        "  code:\n"
        "    model: gpt-test-snapshot\n",
        encoding="utf-8",
    )
    local_env = bot_dir / "local.env"
    local_env.write_text(
        "export CHATCOPILOT_TEST_SNAPSHOT=initial\n"
        "export CHATCOPILOT_CODEX_BOT_HOME=$HOME/codex-bot\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(repository))
    monkeypatch.delenv("CHATCOPILOT_TEST_SNAPSHOT", raising=False)
    monkeypatch.delenv("CHATCOPILOT_EVALUATION_ENV_SNAPSHOT", raising=False)
    observed: dict[str, str] = {}

    def _run(_args: object, _request: dict[str, object]) -> int:
        observed["before"] = os.environ["CHATCOPILOT_TEST_SNAPSHOT"]
        observed["marker"] = os.environ["CHATCOPILOT_EVALUATION_ENV_SNAPSHOT"]
        observed["model"] = os.environ["CHATCOPILOT_TESTBOT_CODE_MODEL"]
        observed["auth_root"] = os.environ["CHATCOPILOT_CODEX_BOT_HOME"]
        local_env.write_text(
            "export CHATCOPILOT_TEST_SNAPSHOT=changed-after-preflight\n",
            encoding="utf-8",
        )
        evaluation_runner._load_local_env(local_env)
        observed["after"] = os.environ["CHATCOPILOT_TEST_SNAPSHOT"]
        return 0

    monkeypatch.setattr(eval_cli_module, "_run_prepared_request", _run)

    assert (
        evals_cli_main(
            [
                "run",
                "--suite",
                "ifeval",
                "--bot",
                "example",
                "--validate-only",
            ]
        )
        == 0
    )
    assert observed == {
        "before": "initial",
        "marker": "1",
        "model": "gpt-test-snapshot",
        "auth_root": str(Path.home() / "codex-bot"),
        "after": "initial",
    }
    assert "CHATCOPILOT_TEST_SNAPSHOT" not in os.environ
    assert "CHATCOPILOT_EVALUATION_ENV_SNAPSHOT" not in os.environ


def test_cli_environment_error_does_not_echo_local_env_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _write_repository_markers(tmp_path / "repo")
    bot_dir = repository / "bots/example"
    bot_dir.mkdir(parents=True)
    (bot_dir / "bot.yaml").write_text("id: example\n", encoding="utf-8")
    secret = "private-unclosed-local-env-value"
    (bot_dir / "local.env").write_text(
        f'export CHATCOPILOT_PRIVATE_FIXTURE="{secret}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(repository))

    code = evals_cli_main(
        [
            "run",
            "--suite",
            "ifeval",
            "--bot",
            "example",
            "--validate-only",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    payload = json.loads(captured.err)
    assert payload == {
        "code": "evaluation_environment_invalid",
        "message": (
            "Bot-local Evaluation environment is invalid; "
            "inspect local.env syntax and configuration."
        ),
        "checks": [],
    }
    assert secret not in captured.err
    assert secret not in captured.out


def test_cli_requires_explicit_output_outside_managed_service_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _write_repository_markers(tmp_path / "repo")
    console = repository / "console"
    console.mkdir()
    monkeypatch.chdir(console)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(repository))
    monkeypatch.delenv("CHATCOPILOT_EVALUATION_ROOT", raising=False)
    arguments = [
        "run",
        "--suite",
        "ifeval",
        "--dry-run",
        "--case-id",
        "ifeval-json-format",
        "--json",
    ]

    missing_code = evals_cli_main(arguments)
    missing = json.loads(capsys.readouterr().err)

    assert missing_code == 2
    assert missing["code"] == "evaluation_output_required"
    assert not (repository / "reports").exists()

    reserved = repository / "reports/evals/evaluations/eval-reserved"
    reserved_code = evals_cli_main(
        [*arguments, "--evaluation-id", reserved.name, "--output", str(reserved)]
    )
    rejected = json.loads(capsys.readouterr().err)

    assert reserved_code == 2
    assert rejected["code"] == "evaluation_output_reserved"
    assert not reserved.exists()


@pytest.mark.parametrize(
    "output_value",
    (
        "../reports/evals/evaluations/eval-relative-reserved",
        "{absolute}",
    ),
)
def test_managed_root_detection_is_stable_from_repository_subdirectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_value: str,
) -> None:
    repository = _write_repository_markers(tmp_path / "repo")
    console = repository / "console"
    console.mkdir()
    monkeypatch.chdir(console)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(repository))
    monkeypatch.delenv("CHATCOPILOT_EVALUATION_ROOT", raising=False)
    absolute = repository / "reports/evals/evaluations/eval-absolute-reserved"
    output = Path(output_value.format(absolute=absolute))

    assert evaluation_paths.managed_evaluation_root() == (repository / "reports/evals/evaluations")
    assert evaluation_paths.is_managed_evaluation_output(output)
    code = evals_cli_main(
        [
            "run",
            "--suite",
            "ifeval",
            "--dry-run",
            "--case-id",
            "ifeval-json-format",
            "--evaluation-id",
            output.name,
            "--output",
            str(output),
            "--json",
        ]
    )
    rejected = json.loads(capsys.readouterr().err)

    assert code == 2
    assert rejected["code"] == "evaluation_output_reserved"
    assert not output.exists()


def test_relative_configured_managed_root_is_anchored_to_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _write_repository_markers(tmp_path / "repo")
    console = repository / "console"
    console.mkdir()
    monkeypatch.chdir(console)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(repository))
    monkeypatch.setenv("CHATCOPILOT_EVALUATION_ROOT", "var/evaluations")
    output = repository / "var/evaluations/eval-configured-relative"

    assert evaluation_paths.managed_evaluation_root() == repository / "var/evaluations"
    assert evaluation_paths.is_managed_evaluation_output(output)


def test_cli_allows_manual_root_from_repository_subdirectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _write_repository_markers(tmp_path / "repo")
    console = repository / "console"
    console.mkdir()
    monkeypatch.chdir(console)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(repository))
    monkeypatch.delenv("CHATCOPILOT_EVALUATION_ROOT", raising=False)
    output = Path("../reports/evals/manual/eval-manual")

    code = evals_cli_main(
        [
            "run",
            "--suite",
            "ifeval",
            "--dry-run",
            "--case-id",
            "ifeval-json-format",
            "--evaluation-id",
            output.name,
            "--output",
            str(output),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert json.loads(captured.out)["evaluation_id"] == output.name
    assert (repository / "reports/evals/manual/eval-manual/result.json").is_file()


def test_managed_root_discovery_fails_closed_without_trusted_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.delenv("CHATCOPILOT_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("CHATCOPILOT_EVALUATION_ROOT", raising=False)
    monkeypatch.setattr(evaluation_paths, "__file__", str(outside / "paths.py"))

    with pytest.raises(RuntimeError, match="cannot locate a trusted"):
        evaluation_paths.managed_evaluation_root()


def test_configured_source_root_must_be_a_valid_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = tmp_path / "not-a-repository"
    invalid.mkdir()
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(invalid))

    with pytest.raises(RuntimeError, match="must contain pyproject.toml"):
        evaluation_paths.managed_evaluation_root()


def test_cli_rejects_configured_managed_service_root_without_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    managed_root = tmp_path / "configured-evaluations"
    managed_root.mkdir()
    sentinel = managed_root / "keep.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    monkeypatch.setenv("CHATCOPILOT_EVALUATION_ROOT", str(managed_root))
    output = managed_root / "eval-configured-cli"

    code = evals_cli_main(
        [
            "run",
            "--suite",
            "ifeval",
            "--dry-run",
            "--case-id",
            "ifeval-json-format",
            "--evaluation-id",
            output.name,
            "--output",
            str(output),
            "--json",
        ]
    )
    rejected = json.loads(capsys.readouterr().err)

    assert code == 2
    assert rejected["code"] == "evaluation_output_reserved"
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert not output.exists()


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
    exit_code = evals_cli_main(["run", "--request", json.dumps(payload), "--validate-only"])
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
    def handler(_arguments: dict, _context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data={})

    allowed = ToolDef(
        name="read_file",
        summary="Read a file.",
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=handler,
    )
    denied = ToolDef(
        name="send_message",
        summary="Send a message.",
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=handler,
    )
    check = permission_filter(frozenset({"read_file"}))

    assert check(allowed) is None
    assert check(denied) == "evaluation policy denies this tool"


def test_isolated_evaluation_excludes_session_bound_persona_pack() -> None:
    assert _isolated_tool_packs(
        ("workspace.read_write", "persona.control", "memory.chat")
    ) == ("workspace.read_write", "memory.chat")


def test_isolated_evaluation_tool_uses_runtime_provider_and_structured_result() -> None:
    case = get_profile("agent-comparison-mvp").cases[2].case
    audit: list[dict[str, object]] = []

    provider = _evaluation_tool_provider(case, audit)

    assert provider is not None
    assert provider.pack_names == ("runtime.session",)
    tool = provider.packs["runtime.session"][0]
    result = tool.handler(
        {"key": case.metadata["expected_key"]},
        ToolContext(),
    )
    assert result.ok is True
    assert result.summary == case.metadata["expected_answer"]
    assert result.data == {"answer": case.metadata["expected_answer"]}
    assert audit == [
        {
            "name": "lookup_eval_fact",
            "arguments": {"key": case.metadata["expected_key"]},
            "ok": True,
        }
    ]


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
