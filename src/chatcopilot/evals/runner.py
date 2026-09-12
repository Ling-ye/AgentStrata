"""Evaluation runner for built-in and external benchmark suites."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from chatcopilot.evals.execution_support import (
    load_local_env as _load_local_env,  # noqa: F401 - public utility alias
)
from chatcopilot.evals.models import (
    EvalCase,
    EvalCaseResult,
    EvalRunResult,
    RunStatus,
)
from chatcopilot.evals.plugins import CaseLoadContext, get_evaluation_plugin
from chatcopilot.evals.registry import get_manifest, get_standard

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
    if manifest.status == "retired":
        raise ValueError("评测集已退出，历史只读；请新建 Agent 任务能力测评。")
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
    from chatcopilot.evals.trial_runner import run_case
    root = workspace_root or ((output / "workspace") if output else Path("reports/evals/workspaces") / suite_id)
    results = []
    if not dry_run and plugin.preflight is not None:
        plugin.preflight(cases=cases)
    for index, case in enumerate(cases, 1):
        _case_started(progress_callback, index=index, total=len(cases), case=case)
        result = run_case(case, suite_id=suite_id, bot=bot or "", workspace_root=root / case.case_id,
                          options=plugin_options, driver=str(manifest.driver_id), dry_run=dry_run,
                          confirm_external_write=confirm_external_write)
        results.append(result)
        _case_completed(progress_callback, index=index, total=len(cases), result=result)
        _write_case_checkpoint(results=results, total_cases=len(cases), suite_id=suite_id, bot=bot,
                               started_at=started_at, suite_start=started, output=output)
        if result.error and result.error.fatal:
            break
    case_results = tuple(results)

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




# ---------------------------------------------------------------------------
# Agent path (GAIA, IFEval, etc.)
# ---------------------------------------------------------------------------


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
