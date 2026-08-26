"""Evaluation runner for built-in and external benchmark suites."""

from __future__ import annotations

import sys
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from chatcopilot.application.agent_runtime import (
    AgentRuntimeAssemblyProfile,
    assemble_agent_runtime,
)
from chatcopilot.core.config import ChatConfig, load_config
from chatcopilot.contracts.agent import AgentTask
from chatcopilot.agent.context.prompt_plan import PromptBuildInput
from chatcopilot.botspec import assemble_runtime_context, load_botspec, resolve_bot_spec_path
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.evals.execution_support import (
    event_to_dict as _event_to_dict,
    load_local_env as _load_local_env,
    usage_summary as _usage_summary,
)
from chatcopilot.evals.models import (
    EvalCase,
    EvalCaseResult,
    EvalRunResult,
    JudgeResult,
    RunStatus,
)
from chatcopilot.evals.plugins import CaseLoadContext, EvaluationPlugin, get_evaluation_plugin
from chatcopilot.evals.registry import get_manifest, get_standard
from chatcopilot.core.workspace_runtime import MiddlewareWorkspaceService
from chatcopilot.project import ENV_PREFIX

ProgressCallback = Callable[[dict[str, Any]], None]


def run_suite(
    suite_id: str,
    *,
    bot: str | None = None,
    output: Path | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    llm_judge: bool = False,
    category: str | None = None,
    case_ids: list[str] | tuple[str, ...] | None = None,
    progress_callback: ProgressCallback | None = None,
    workspace_root: Path | None = None,
    options: dict[str, Any] | None = None,
    confirm_external_write: bool = False,
    _frozen_cases: tuple[EvalCase, ...] | None = None,
) -> EvalRunResult:
    """Run one suite and optionally write a report directory."""

    standard = get_standard(suite_id)
    manifest = get_manifest(standard.suite_id)
    plugin = get_evaluation_plugin(manifest.plugin_id)
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()

    plugin_options = dict(options or {})
    if category is not None:
        plugin_options["category"] = category
    if _frozen_cases is None:
        loaded_cases = plugin.load_cases(
            CaseLoadContext(
                manifest=manifest,
                auto_prepare=True,
                options=plugin_options,
            )
        )
    else:
        if not _frozen_cases or any(not isinstance(case, EvalCase) for case in _frozen_cases):
            raise ValueError("frozen suite Cases must be a non-empty tuple of EvalCase")
        frozen_ids = tuple(case.case_id for case in _frozen_cases)
        if len(set(frozen_ids)) != len(frozen_ids):
            raise ValueError("frozen suite Case ids must be unique")
        loaded_cases = _frozen_cases
    cases = _select_cases(loaded_cases, case_ids=case_ids, limit=limit)

    if standard.requires_external_data and not cases:
        result = EvalRunResult(
            suite_id=standard.suite_id,
            bot=bot,
            status="unavailable",
            started_at=started_at,
            duration_seconds=0.0,
            summary={"reason": "requires_external_data", "setup_hint": standard.setup_hint},
            error=f"{standard.name} 需要外部官方数据集，当前未配置可运行 cases。",
        )
        if output is not None:
            from chatcopilot.evals.report import write_run_report

            write_run_report(result, output)
        return result

    if not cases:
        result = EvalRunResult(
            suite_id=standard.suite_id,
            bot=bot,
            status="unavailable",
            started_at=started_at,
            duration_seconds=0.0,
            error=f"{standard.name} 没有可运行 cases。",
        )
        if output is not None:
            from chatcopilot.evals.report import write_run_report

            write_run_report(result, output)
        return result

    _emit_progress(
        progress_callback,
        event="suite_started",
        suite_id=standard.suite_id,
        total=len(cases),
    )
    effective_driver = "dry_run" if dry_run else manifest.driver_id
    if effective_driver == "dry_run":
        case_results = tuple(
            _run_dry_cases(
                standard.suite_id,
                cases,
                bot=bot,
                output=output,
                started_at=started_at,
                suite_start=started,
                progress_callback=progress_callback,
            )
        )
    elif effective_driver == "direct_llm":
        if not bot:
            raise ValueError(f"{standard.name} 需要 --bot 指定 BotSpec（用于 LLM 配置）。")
        case_results = tuple(
            _run_direct_llm_cases(
                standard.suite_id,
                plugin,
                cases,
                bot=bot,
                output=output,
                started_at=started_at,
                suite_start=started,
                progress_callback=progress_callback,
            )
        )
    elif any(isinstance(case.metadata.get("case_definition"), dict) for case in cases):
        if standard.requires_bot and not bot:
            raise ValueError(f"{standard.name} 需要 --bot 指定 BotSpec。")
        case_results = tuple(
            _run_declarative_cases(
                standard.suite_id,
                cases,
                bot=bot or "",
                output=output,
                started_at=started_at,
                suite_start=started,
                progress_callback=progress_callback,
                workspace_root=workspace_root,
                options=plugin_options,
                confirm_external_write=confirm_external_write,
            )
        )
    elif effective_driver == "agent_configured":
        if standard.requires_bot and not bot:
            raise ValueError(f"{standard.name} 需要 --bot 指定 BotSpec。")
        case_results = tuple(
            _run_agent_cases(
                standard.suite_id,
                cases,
                plugin=plugin,
                bot=bot or "",
                llm_judge=llm_judge,
                output=output,
                started_at=started_at,
                suite_start=started,
                progress_callback=progress_callback,
                workspace_root=workspace_root,
            )
        )
    else:
        raise ValueError(f"suite {standard.suite_id} has no Core driver for {effective_driver!r}")

    duration = time.monotonic() - started
    result = EvalRunResult(
        suite_id=standard.suite_id,
        bot=bot,
        status=_aggregate_status(case_results),
        started_at=started_at,
        duration_seconds=duration,
        cases=case_results,
        summary=_summarize(case_results),
    )
    if output is not None:
        from chatcopilot.evals.report import write_run_report

        write_run_report(result, output)
    _emit_progress(
        progress_callback,
        event="suite_completed",
        suite_id=standard.suite_id,
        total=len(cases),
        completed=len(case_results),
        status=result.status,
    )
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Direct-LLM plugin path (function-call protocol calibration, no Agent loop)
# ---------------------------------------------------------------------------


def _run_direct_llm_cases(
    suite_id: str,
    plugin: EvaluationPlugin,
    cases: tuple[EvalCase, ...],
    *,
    bot: str,
    output: Path | None = None,
    started_at: str = "",
    suite_start: float = 0.0,
    progress_callback: ProgressCallback | None = None,
) -> list[EvalCaseResult]:
    """Run one trusted direct-LLM plugin without assuming a benchmark identity."""

    if plugin.execute_trial is None or plugin.judge is None:
        raise ValueError(
            f"direct_llm plugin {plugin.plugin_id!r} must define execute_trial and judge hooks"
        )

    chat_config = _load_bot_config(bot)
    results: list[EvalCaseResult] = []

    total = len(cases)
    for index, case in enumerate(cases, start=1):
        _case_started(progress_callback, index=index, total=total, case=case)
        started = time.monotonic()
        case_started_at = _utc_now()
        try:
            observation = plugin.execute_trial(case, chat_config=chat_config)
            if not isinstance(observation, dict):
                raise TypeError(
                    f"direct_llm plugin {plugin.plugin_id!r} returned a non-mapping observation"
                )
            final_text = str(observation.get("final_text") or "")
            tool_calls = observation.get("tool_calls") or []
            usage = observation.get("usage") or {}
            plugin_metadata = observation.get("metadata") or {}
            if (
                not isinstance(tool_calls, list)
                or not isinstance(usage, dict)
                or not isinstance(plugin_metadata, dict)
            ):
                raise TypeError(
                    f"direct_llm plugin {plugin.plugin_id!r} returned an invalid observation"
                )
            judge_result = plugin.judge(case, observation)
            if not isinstance(judge_result, JudgeResult):
                raise TypeError(
                    f"direct_llm plugin {plugin.plugin_id!r} returned an invalid judge result"
                )
            results.append(
                EvalCaseResult(
                    case_id=case.case_id,
                    suite_id=suite_id,
                    status="passed" if judge_result.passed else "failed",
                    score=judge_result.score,
                    max_score=judge_result.max_score,
                    final_text=final_text,
                    stop_reason="end_turn",
                    duration_seconds=time.monotonic() - started,
                    started_at=case_started_at,
                    finished_at=_utc_now(),
                    judge=judge_result,
                    metadata={
                        **plugin_metadata,
                        "usage_totals": _flatten_usage(usage),
                        "tool_calls": tool_calls,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                EvalCaseResult(
                    case_id=case.case_id,
                    suite_id=suite_id,
                    status="error",
                    duration_seconds=time.monotonic() - started,
                    started_at=case_started_at,
                    finished_at=_utc_now(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        _case_completed(
            progress_callback,
            index=index,
            total=total,
            result=results[-1],
        )
        _write_case_checkpoint(
            results=results,
            total_cases=total,
            suite_id=suite_id,
            bot=bot,
            started_at=started_at,
            suite_start=suite_start,
            output=output,
        )

    return results


def _flatten_usage(usage: Any) -> dict[str, int]:
    if isinstance(usage, dict):
        totals: dict[str, int] = {}
        for key, value in usage.items():
            if isinstance(value, int):
                totals[key] = value
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, int):
                        totals[nested_key] = totals.get(nested_key, 0) + nested_value
                        totals[f"{key}.{nested_key}"] = nested_value
        return totals
    if hasattr(usage, "model_dump"):
        return _flatten_usage(usage.model_dump())
    return {}


# ---------------------------------------------------------------------------
# Agent path (GAIA, IFEval, etc.)
# ---------------------------------------------------------------------------


def _run_declarative_cases(
    suite_id: str,
    cases: tuple[EvalCase, ...],
    *,
    bot: str,
    output: Path | None,
    started_at: str,
    suite_start: float,
    progress_callback: ProgressCallback | None,
    workspace_root: Path | None,
    options: dict[str, Any],
    confirm_external_write: bool,
) -> list[EvalCaseResult]:
    """Execute strict repository-owned Cases through the capability driver.

    The driver returns ordinary ``EvalCaseResult`` values only.  Lifecycle,
    authoritative artifacts, checkpointing, redaction and cancellation remain
    owned by Evaluation Core.
    """

    from chatcopilot.evals.capability_executor import execute_capability_case

    root = (
        workspace_root.resolve()
        if workspace_root is not None
        else (Path("reports") / "evals" / "workspaces" / suite_id).resolve()
    )
    total = len(cases)
    results: list[EvalCaseResult] = []
    for index, case in enumerate(cases, start=1):
        _case_started(progress_callback, index=index, total=total, case=case)
        case_workspace = root if total == 1 else root / case.case_id
        result = execute_capability_case(
            case,
            suite_id=suite_id,
            bot=bot,
            workspace_root=case_workspace,
            options=options,
            confirm_external_write=confirm_external_write,
        )
        results.append(result)
        _case_completed(progress_callback, index=index, total=total, result=result)
        _write_case_checkpoint(
            results=results,
            total_cases=total,
            suite_id=suite_id,
            bot=bot,
            started_at=started_at,
            suite_start=suite_start,
            output=output,
        )
    return results


def _run_agent_cases(
    suite_id: str,
    cases: tuple[EvalCase, ...],
    *,
    plugin: EvaluationPlugin | None = None,
    bot: str,
    llm_judge: bool = False,
    output: Path | None = None,
    started_at: str = "",
    suite_start: float = 0.0,
    progress_callback: ProgressCallback | None = None,
    workspace_root: Path | None = None,
) -> list[EvalCaseResult]:
    plugin = plugin or get_evaluation_plugin(get_manifest(suite_id).plugin_id)
    runtime = assemble_runtime_context(
        load_botspec(resolve_bot_spec_path(Path(bot) if _looks_like_path(bot) else bot))
    )
    _load_local_env(runtime.source_path.parent / "local.env")
    chat_config = load_config(env_prefix=runtime.spec.llm.env_prefix)
    agent_runtime = assemble_agent_runtime(
        runtime,
        chat_config=chat_config,
        profile=AgentRuntimeAssemblyProfile.DETACHED,
    )
    try:
        resolved_workspace_root = workspace_root or (
            (output / "workspace")
            if output is not None
            else Path("reports") / "evals" / "workspaces" / runtime.instance_id
        )
        workspace = Workspace(
            root=resolved_workspace_root.resolve(),
            chat_kind="p2p",
            chat_id=f"eval:{suite_id}",
            user_id="eval-user",
            user_name="Eval Runner",
        ).ensure()
        env_guard = _EvalWorkspaceEnv(workspace)
        total = len(cases)
        with env_guard:
            results: list[EvalCaseResult] = []
            for index, case in enumerate(cases, start=1):
                _case_started(progress_callback, index=index, total=total, case=case)
                session = agent_runtime.new_session(
                    session_id=f"eval-{suite_id}-{case.case_id}-{index}",
                    prompt_input=PromptBuildInput(
                        profile=runtime.prompt_profile,
                        backend=runtime.agent_backend,
                        model=None,
                        role="owner",
                        channel_kind="private",
                        session_policy="这是隔离 Evaluation 会话；只处理当前评测 Case。",
                        capability_policies=runtime.capability_policies,
                        skill_index=runtime.skills,
                    ),
                    workspace_service=MiddlewareWorkspaceService(),
                )
                events: list[dict[str, Any]] = []
                case_start = time.monotonic()
                case_started_at = _utc_now()
                try:
                    task = _prepare_task(plugin, suite_id, case, workspace)
                    agent_result = session.run_task(
                        task,
                        on_event=lambda event: events.append(_event_to_dict(event)),
                    )
                    judge = _judge_case(
                        plugin,
                        case,
                        agent_result.final_text,
                        chat_config=chat_config if llm_judge else None,
                    )
                    case_result = EvalCaseResult(
                        case_id=case.case_id,
                        suite_id=suite_id,
                        status="passed" if judge.passed else "failed",
                        score=judge.score,
                        max_score=judge.max_score,
                        final_text=agent_result.final_text,
                        stop_reason=agent_result.stop_reason,
                        duration_seconds=time.monotonic() - case_start,
                        started_at=case_started_at,
                        finished_at=_utc_now(),
                        events=tuple(events),
                        judge=judge,
                        metadata=_usage_summary(events),
                    )
                except Exception as exc:  # noqa: BLE001
                    case_result = EvalCaseResult(
                        case_id=case.case_id,
                        suite_id=suite_id,
                        status="error",
                        duration_seconds=time.monotonic() - case_start,
                        started_at=case_started_at,
                        finished_at=_utc_now(),
                        events=tuple(events),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                results.append(case_result)
                _case_completed(
                    progress_callback,
                    index=index,
                    total=total,
                    result=case_result,
                )
                _write_case_checkpoint(
                    results=results,
                    total_cases=total,
                    suite_id=suite_id,
                    bot=bot,
                    started_at=started_at,
                    suite_start=suite_start,
                    output=output,
                )
            return results
    finally:
        agent_runtime.close()


def _run_dry_cases(
    suite_id: str,
    cases: tuple[EvalCase, ...],
    *,
    bot: str | None,
    output: Path | None,
    started_at: str,
    suite_start: float,
    progress_callback: ProgressCallback | None,
) -> list[EvalCaseResult]:
    results: list[EvalCaseResult] = []
    total = len(cases)
    for index, case in enumerate(cases, start=1):
        _case_started(progress_callback, index=index, total=total, case=case)
        result = _dry_run_case(suite_id, case)
        results.append(result)
        _case_completed(progress_callback, index=index, total=total, result=result)
        _write_case_checkpoint(
            results=results,
            total_cases=total,
            suite_id=suite_id,
            bot=bot,
            started_at=started_at,
            suite_start=suite_start,
            output=output,
        )
    return results


def _write_case_checkpoint(
    *,
    results: list[EvalCaseResult],
    total_cases: int,
    suite_id: str,
    bot: str | None,
    started_at: str,
    suite_start: float,
    output: Path | None,
) -> None:
    if output is None:
        return
    _write_checkpoint(
        results=tuple(results),
        total_cases=total_cases,
        suite_id=suite_id,
        bot=bot,
        started_at=started_at,
        elapsed=time.monotonic() - (suite_start or time.monotonic()),
        output=output,
    )


def _case_started(
    callback: ProgressCallback | None,
    *,
    index: int,
    total: int,
    case: EvalCase,
) -> None:
    print(f"[{index}/{total}] running {case.case_id} ...", file=sys.stderr, flush=True)
    _emit_progress(
        callback,
        event="case_started",
        case_id=case.case_id,
        index=index,
        total=total,
    )


def _case_completed(
    callback: ProgressCallback | None,
    *,
    index: int,
    total: int,
    result: EvalCaseResult,
) -> None:
    print(
        f"[{index}/{total}] {result.status} {result.case_id} "
        f"{result.duration_seconds:.1f}s score={result.score:.0f}/{result.max_score:.0f}",
        file=sys.stderr,
        flush=True,
    )
    _emit_progress(
        callback,
        event="case_completed",
        case_id=result.case_id,
        index=index,
        total=total,
        status=result.status,
        score=result.score,
        max_score=result.max_score,
        duration_seconds=result.duration_seconds,
    )


def _emit_progress(
    callback: ProgressCallback | None,
    **payload: Any,
) -> None:
    if callback is not None:
        callback(dict(payload))


def _write_checkpoint(
    results: tuple[EvalCaseResult, ...],
    total_cases: int,
    suite_id: str,
    bot: str | None,
    started_at: str,
    elapsed: float,
    output: Path,
) -> None:
    """Write an intermediate checkpoint after each case completes."""
    from chatcopilot.evals.report import write_run_report

    completed = len(results)
    summary = _summarize(results)
    summary.update({"completed_cases": completed, "total_cases": total_cases})
    partial = EvalRunResult(
        suite_id=suite_id,
        bot=bot,
        status="running",
        started_at=started_at,
        duration_seconds=elapsed,
        cases=results,
        summary=summary,
    )
    try:
        write_run_report(partial, output)
    except Exception:  # noqa: BLE001
        pass


def _dry_run_case(suite_id: str, case: EvalCase) -> EvalCaseResult:
    now = _utc_now()
    return EvalCaseResult(
        case_id=case.case_id,
        suite_id=suite_id,
        status="skipped",
        score=0.0,
        max_score=1.0,
        started_at=now,
        finished_at=now,
        error="dry-run: validated case shape but did not call the agent",
    )


def _judge_case(
    plugin: EvaluationPlugin,
    case: EvalCase,
    final_text: str,
    *,
    chat_config: ChatConfig | None = None,
) -> JudgeResult:
    if plugin.judge is None:
        raise ValueError(
            f"agent plugin {plugin.plugin_id!r} must define a deterministic judge hook"
        )
    result = plugin.judge(case, final_text, chat_config=chat_config)
    if not isinstance(result, JudgeResult):
        raise TypeError(f"evaluation plugin {plugin.plugin_id!r} returned an invalid judge result")
    return result


def _load_bot_config(bot: str) -> ChatConfig:
    """Load ChatConfig from a BotSpec path (for BFCL and other LLM-only paths)."""

    runtime = assemble_runtime_context(
        load_botspec(resolve_bot_spec_path(Path(bot) if _looks_like_path(bot) else bot))
    )
    _load_local_env(runtime.source_path.parent / "local.env")
    return load_config(env_prefix=runtime.spec.llm.env_prefix)


def _prepare_task(
    plugin: EvaluationPlugin,
    suite_id: str,
    case: EvalCase,
    workspace: Workspace,
) -> AgentTask:
    if plugin.build_task is not None:
        task = plugin.build_task(case, workspace)
        if not isinstance(task, AgentTask):
            raise TypeError(f"evaluation plugin {plugin.plugin_id!r} returned an invalid AgentTask")
        return task
    return AgentTask(
        text=case.input,
        turn_context=_case_context(case),
        metadata={"eval_suite": suite_id, "eval_case": case.case_id},
    )


def _case_context(case: EvalCase) -> str:
    parts = [
        "## Eval Case Context",
        f"case_id: {case.case_id}",
        f"category: {case.category}",
        f"expected_behavior: {case.expected_behavior}",
    ]
    if case.context:
        parts.append(f"context: {case.context}")
    if case.rubric:
        parts.append(f"rubric: {case.rubric}")
    return "\n".join(parts)


def _summarize(results: tuple[EvalCaseResult, ...]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")
    errors = sum(1 for item in results if item.status == "error")
    skipped = sum(1 for item in results if item.status == "skipped")
    score = sum(item.score for item in results)
    max_score = sum(item.max_score for item in results) or 1.0
    usage_totals = _summarize_usage(results)
    summary: dict[str, Any] = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "score": score,
        "max_score": max_score,
        "score_ratio": score / max_score,
    }
    if usage_totals:
        summary["usage_totals"] = usage_totals
        summary["cost_estimates"] = {
            "deepseek_v4_pro_rmb": _estimate_deepseek_v4_pro_cost(usage_totals, total_cases=total)
        }

    leaderboard = _leaderboard_format(results, score / max_score)
    if leaderboard:
        summary["leaderboard"] = leaderboard

    return summary


def _leaderboard_format(
    results: tuple[EvalCaseResult, ...],
    overall_accuracy: float,
) -> dict[str, Any] | None:
    """Build leaderboard-comparable metrics keyed by suite convention."""

    if not results:
        return None

    suite_ids = {r.suite_id for r in results if r.suite_id}
    if not suite_ids:
        return None

    suite_id = suite_ids.pop() if len(suite_ids) == 1 else "mixed"
    evaluated = [r for r in results if r.status not in ("skipped", "error")]
    if not evaluated:
        return None

    entry: dict[str, Any] = {
        "suite": suite_id,
        "accuracy": round(overall_accuracy, 4),
        "n_evaluated": len(evaluated),
    }

    categories: dict[str, dict[str, float]] = {}
    for result in evaluated:
        if not isinstance(result.metadata, dict):
            continue
        category = str(result.metadata.get("benchmark_category") or "").strip()
        if not category:
            continue
        values = categories.setdefault(category, {"score": 0.0, "total": 0.0})
        values["score"] += result.score
        values["total"] += result.max_score
    for category, values in sorted(categories.items()):
        entry[f"accuracy_{category}"] = (
            round(values["score"] / values["total"], 4) if values["total"] else 0.0
        )

    return entry


def _summarize_usage(results: tuple[EvalCaseResult, ...]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for item in results:
        usage = item.metadata.get("usage_totals") if isinstance(item.metadata, dict) else None
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return totals


def _estimate_deepseek_v4_pro_cost(
    usage_totals: dict[str, int], *, total_cases: int
) -> dict[str, Any]:
    prompt_tokens = _usage_value(usage_totals, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_value(usage_totals, "completion_tokens", "output_tokens")
    reasoning_tokens = _usage_value(
        usage_totals,
        "reasoning_tokens",
        "completion_tokens_details.reasoning_tokens",
    )
    cached_tokens = _usage_value(
        usage_totals,
        "cached_tokens",
        "prompt_cache_hit_tokens",
        "cache_read_input_tokens",
        "prompt_tokens_details.cached_tokens",
    )
    cached_tokens = min(cached_tokens, prompt_tokens)
    uncached_tokens = max(prompt_tokens - cached_tokens, 0)
    cost_rmb = (
        (uncached_tokens / 1_000_000 * 3.0)
        + (cached_tokens / 1_000_000 * 0.025)
        + (completion_tokens / 1_000_000 * 6.0)
    )
    return {
        "model": "deepseek-v4-pro",
        "input_uncached_rmb_per_1m": 3.0,
        "input_cached_rmb_per_1m": 0.025,
        "output_rmb_per_1m": 6.0,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "uncached_tokens": uncached_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "estimated_rmb": round(cost_rmb, 6),
        "estimated_rmb_per_case": round(cost_rmb / total_cases, 6) if total_cases else 0.0,
    }


def _usage_value(usage_totals: dict[str, int], *keys: str) -> int:
    for key in keys:
        value = usage_totals.get(key)
        if isinstance(value, int):
            return value
    return 0


def _aggregate_status(results: tuple[EvalCaseResult, ...]) -> RunStatus:
    if not results:
        return "unavailable"
    if any(item.status == "error" for item in results):
        return "error"
    if any(item.status == "failed" for item in results):
        return "failed"
    if all(item.status == "skipped" for item in results):
        return "skipped"
    return "passed"


def _limited_cases(cases: tuple[EvalCase, ...], limit: int | None) -> tuple[EvalCase, ...]:
    if limit is None or limit <= 0:
        return cases
    return cases[:limit]


def _select_cases(
    cases: tuple[EvalCase, ...],
    *,
    case_ids: list[str] | tuple[str, ...] | None,
    limit: int | None,
) -> tuple[EvalCase, ...]:
    if case_ids is None:
        return _limited_cases(cases, limit)
    if limit is not None:
        raise ValueError("case_ids and limit cannot be used together")
    normalized = [str(item).strip() for item in case_ids]
    if not normalized or any(not item for item in normalized):
        raise ValueError("case_ids must contain at least one non-empty case id")
    if len(set(normalized)) != len(normalized):
        raise ValueError("case_ids contains duplicate values")
    requested = set(normalized)
    known = {case.case_id for case in cases}
    unknown = sorted(requested - known)
    if unknown:
        raise ValueError(f"unknown case_ids: {', '.join(unknown)}")
    return tuple(case for case in cases if case.case_id in requested)


def _looks_like_path(value: str) -> bool:
    return any(sep in value for sep in ("/", "\\")) or value.endswith((".yaml", ".yml"))


class _EvalWorkspaceEnv:
    def __init__(self, workspace: Workspace) -> None:
        self._values = {
            f"{ENV_PREFIX}_WORKSPACE": str(workspace.root),
            f"{ENV_PREFIX}_CHAT_KIND": workspace.chat_kind or "",
            f"{ENV_PREFIX}_CHAT_ID": workspace.chat_id or "",
            f"{ENV_PREFIX}_USER_ID": workspace.user_id or "",
            f"{ENV_PREFIX}_USER_NAME": workspace.user_name or "",
        }
        self._old: dict[str, str | None] = {}

    def __enter__(self) -> "_EvalWorkspaceEnv":
        for key, value in self._values.items():
            self._old[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        for key, old_value in self._old.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
