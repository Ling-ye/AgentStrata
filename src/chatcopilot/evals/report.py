"""Report writers and baseline comparison for evaluation runs."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from chatcopilot.evals.models import EvalRunResult, to_jsonable


def write_run_report(result: EvalRunResult, output: Path) -> None:
    """Write machine-readable and human-readable run artifacts."""

    output.mkdir(parents=True, exist_ok=True)
    payload = to_jsonable(result)
    _atomic_write_text(
        output / "result.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    failures = [
        item
        for item in payload.get("cases", [])
        if item.get("status") in {"failed", "error", "unavailable"}
    ]
    _atomic_write_text(
        output / "failures.jsonl",
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures),
    )
    _atomic_write_text(output / "summary.md", render_summary_markdown(result))
    index_payload = {
        "suite_id": payload.get("suite_id"),
        "bot": payload.get("bot"),
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "duration_seconds": payload.get("duration_seconds"),
        "summary": payload.get("summary", {}),
        "error": payload.get("error", ""),
        "cases": [
            {
                "case_id": item.get("case_id"),
                "status": item.get("status"),
                "score": item.get("score"),
                "max_score": item.get("max_score"),
                "duration_seconds": item.get("duration_seconds"),
                "started_at": item.get("started_at", ""),
                "finished_at": item.get("finished_at", ""),
                "error": item.get("error", ""),
            }
            for item in payload.get("cases", [])
            if isinstance(item, dict)
        ],
    }
    _atomic_write_text(
        output / "index.json",
        json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n",
    )


def _atomic_write_text(path: Path, content: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def render_summary_markdown(result: EvalRunResult) -> str:
    summary = result.summary
    lines = [
        f"# Eval Report: {result.suite_id}",
        "",
        f"- status: `{result.status}`",
        f"- bot: `{result.bot or ''}`",
        f"- started_at: `{result.started_at}`",
        f"- duration_seconds: `{result.duration_seconds:.2f}`",
        f"- total: `{summary.get('total', 0)}`",
        f"- passed: `{summary.get('passed', 0)}`",
        f"- failed: `{summary.get('failed', 0)}`",
        f"- errors: `{summary.get('errors', 0)}`",
        f"- skipped: `{summary.get('skipped', 0)}`",
        f"- score_ratio: `{summary.get('score_ratio', 0):.3f}`",
    ]
    cost = (summary.get("cost_estimates") or {}).get("deepseek_v4_pro_rmb")
    if isinstance(cost, dict):
        lines.extend(
            [
                f"- deepseek_v4_pro_estimated_rmb: `{cost.get('estimated_rmb', 0):.6f}`",
                f"- deepseek_v4_pro_estimated_rmb_per_case: `{cost.get('estimated_rmb_per_case', 0):.6f}`",
                f"- prompt_tokens: `{cost.get('prompt_tokens', 0)}`",
                f"- cached_tokens: `{cost.get('cached_tokens', 0)}`",
                f"- completion_tokens: `{cost.get('completion_tokens', 0)}`",
            ]
        )

    category_groups = _group_by_category(result)
    if category_groups:
        lines.extend(["", "## Scores by Category", ""])
        lines.append("| Category | Cases | Passed | Score | Accuracy |")
        lines.append("|----------|------:|-------:|------:|---------:|")
        for cat, stats in sorted(category_groups.items()):
            accuracy = stats["score"] / stats["max_score"] if stats["max_score"] else 0.0
            lines.append(
                f"| {cat} | {stats['total']} | {stats['passed']} "
                f"| {stats['score']:.1f}/{stats['max_score']:.1f} "
                f"| {accuracy:.1%} |"
            )

    leaderboard = summary.get("leaderboard")
    if isinstance(leaderboard, dict):
        lines.extend(["", "## Leaderboard Comparable", ""])
        for key, value in leaderboard.items():
            lines.append(f"- {key}: `{value}`")

    lines.extend(["", "## Cases", ""])
    for case in result.cases:
        lines.append(
            f"- `{case.case_id}`: {case.status}, score={case.score:.3f}/{case.max_score:.3f}, "
            f"duration={case.duration_seconds:.2f}s"
        )
        if case.error:
            lines.append(f"  error: {case.error}")
        if case.judge and (case.judge.missing or case.judge.violations):
            lines.append(f"  missing: {', '.join(case.judge.missing) or '-'}")
            lines.append(f"  violations: {', '.join(case.judge.violations) or '-'}")
    return "\n".join(lines) + "\n"


def _group_by_category(result: EvalRunResult) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for case in result.cases:
        cat = _case_category(case)
        if cat not in groups:
            groups[cat] = {"total": 0, "passed": 0, "score": 0.0, "max_score": 0.0}
        groups[cat]["total"] += 1
        groups[cat]["score"] += case.score
        groups[cat]["max_score"] += case.max_score
        if case.status == "passed":
            groups[cat]["passed"] += 1
    return groups if len(groups) > 1 else {}


def _case_category(case: Any) -> str:
    if hasattr(case, "metadata") and isinstance(case.metadata, dict):
        cat = case.metadata.get("bfcl_category")
        if cat:
            return str(cat)
    if hasattr(case, "case_id"):
        case_id = str(case.case_id)
        if case_id.startswith("bfcl-smoke-"):
            parts = case_id.replace("bfcl-smoke-", "").split("-")
            return parts[0] if parts else "unknown"
    suite_id = getattr(case, "suite_id", "") or ""
    return suite_id or "unknown"


def compare_reports(base: Path, new: Path) -> dict[str, Any]:
    """Compare two canonical Evaluation result artifacts."""

    base_payload = _load_result(base)
    new_payload = _load_result(new)
    _require_comparable_evaluations(base_payload, new_payload)
    base_ratio = _evaluation_score_ratio(base_payload)
    new_ratio = _evaluation_score_ratio(new_payload)

    base_cases = _comparison_trials(base_payload)
    new_cases = _comparison_trials(new_payload)
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for case_key, new_case in new_cases.items():
        base_case = base_cases.get(case_key)
        if not base_case:
            continue
        delta = float(new_case["score_ratio"]) - float(base_case["score_ratio"])
        item = {
            "case_ref": new_case["case_ref"],
            "case_id": new_case["case_id"],
            "target_id": new_case["target_id"],
            "base_outcome": base_case["outcome"],
            "new_outcome": new_case["outcome"],
            "score_delta": delta,
        }
        if delta < -0.05 or (
            base_case["outcome"] == "passed"
            and new_case["outcome"] != "passed"
        ):
            regressions.append(item)
        elif delta > 0.05:
            improvements.append(item)

    return {
        "base_evaluation_id": base_payload.get("evaluation_id"),
        "new_evaluation_id": new_payload.get("evaluation_id"),
        "base_kind": base_payload.get("kind"),
        "new_kind": new_payload.get("kind"),
        "base_suite": base_payload.get("suite"),
        "new_suite": new_payload.get("suite"),
        "base_score_ratio": base_ratio,
        "new_score_ratio": new_ratio,
        "score_delta": new_ratio - base_ratio,
        "regressions": regressions,
        "improvements": improvements,
    }


def _require_comparable_evaluations(
    base: dict[str, Any],
    new: dict[str, Any],
) -> None:
    base_kind = str(base.get("kind") or "")
    new_kind = str(new.get("kind") or "")
    if base_kind != new_kind:
        raise ValueError(
            f"Evaluation kinds are not comparable: {base_kind!r} != {new_kind!r}"
        )
    scope_field = "profile" if base_kind == "comparison" else "suite"
    base_scope = str(base.get(scope_field) or "")
    new_scope = str(new.get(scope_field) or "")
    if not base_scope or base_scope != new_scope:
        raise ValueError(
            f"Evaluation {scope_field}s are not comparable: "
            f"{base_scope!r} != {new_scope!r}"
        )
    if base.get("status") != "completed" or new.get("status") != "completed":
        raise ValueError("Only completed Evaluations are comparable")
    base_cases = base.get("selected_cases")
    new_cases = new.get("selected_cases")
    if (
        not isinstance(base_cases, list)
        or not isinstance(new_cases, list)
        or base_cases != new_cases
    ):
        raise ValueError("Evaluation selected_cases are not comparable")
    base_snapshot = base.get("config_snapshot")
    new_snapshot = new.get("config_snapshot")
    if not isinstance(base_snapshot, dict) or not isinstance(new_snapshot, dict):
        raise ValueError("Evaluation config_snapshot is required for comparison")
    for field_name in ("case_hash", "judge"):
        if (
            not base_snapshot.get(field_name)
            or base_snapshot.get(field_name) != new_snapshot.get(field_name)
        ):
            raise ValueError(f"Evaluation {field_name} is not comparable")
    base_targets = _comparable_targets(base)
    new_targets = _comparable_targets(new)
    if base_targets != new_targets:
        raise ValueError(
            "Evaluation Targets are not comparable: "
            f"{base_targets!r} != {new_targets!r}"
        )
    if _trial_sample_keys(base) != _trial_sample_keys(new):
        raise ValueError("Evaluation Trial sample keys are not comparable")


def _comparable_targets(
    payload: dict[str, Any],
) -> dict[str, tuple[str, str]]:
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("Evaluation result.targets must be a non-empty list")
    targets: dict[str, tuple[str, str]] = {}
    for item in raw_targets:
        if not isinstance(item, dict):
            raise ValueError("Evaluation result.targets must contain objects")
        target_id = str(item.get("target_id") or "").strip()
        if not target_id:
            raise ValueError("Evaluation Target must have target_id")
        if target_id in targets:
            raise ValueError("Evaluation result.targets contains duplicate target_id")
        executor = str(item.get("executor") or "").strip()
        backend = str(item.get("backend") or "").strip()
        if not executor or not backend:
            raise ValueError("Evaluation Target must have executor and backend")
        targets[target_id] = (executor, backend)
    return targets


def _trial_sample_keys(payload: dict[str, Any]) -> frozenset[tuple[str, str, int]]:
    keys: list[tuple[str, str, int]] = []
    for index, raw in enumerate(payload.get("trials", [])):
        if not isinstance(raw, dict):
            raise ValueError(f"Evaluation Trial {index} must be an object")
        case_ref = str(raw.get("case_ref") or "").strip()
        target_id = str(raw.get("target_id") or "").strip()
        attempt = raw.get("attempt")
        if (
            not case_ref
            or not target_id
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt <= 0
        ):
            raise ValueError(f"Evaluation Trial {index} sample key is invalid")
        keys.append((case_ref, target_id, attempt))
    if len(set(keys)) != len(keys):
        raise ValueError("Evaluation result.trials contains duplicate sample keys")
    return frozenset(keys)


def render_compare_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Eval Compare",
        "",
        f"- base_suite: `{payload.get('base_suite')}`",
        f"- new_suite: `{payload.get('new_suite')}`",
        f"- base_score_ratio: `{payload.get('base_score_ratio', 0):.3f}`",
        f"- new_score_ratio: `{payload.get('new_score_ratio', 0):.3f}`",
        f"- score_delta: `{payload.get('score_delta', 0):+.3f}`",
        "",
        "## Regressions",
        "",
    ]
    regressions = payload.get("regressions") or []
    if not regressions:
        lines.append("- none")
    else:
        for item in regressions:
            lines.append(
                f"- `{item['case_ref']}`: {item['base_outcome']} -> {item['new_outcome']}, "
                f"delta={item['score_delta']:+.3f}"
            )
    lines.extend(["", "## Improvements", ""])
    improvements = payload.get("improvements") or []
    if not improvements:
        lines.append("- none")
    else:
        for item in improvements:
            lines.append(f"- `{item['case_ref']}`: delta={item['score_delta']:+.3f}")
    return "\n".join(lines) + "\n"


def _load_result(path: Path) -> dict[str, Any]:
    result_path = path / "result.json" if path.is_dir() else path
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{result_path}: Evaluation result must be an object")
    if payload.get("kind") not in {"comparison", "suite"}:
        raise ValueError(f"{result_path}: not a canonical Evaluation result")
    if not isinstance(payload.get("trials"), list):
        raise ValueError(f"{result_path}: Evaluation result.trials must be a list")
    _require_finite_numbers(payload)
    return payload


def _evaluation_score_ratio(payload: dict[str, Any]) -> float:
    summary = payload.get("summary")
    if isinstance(summary, dict) and isinstance(
        summary.get("score_ratio"),
        (int, float),
    ):
        return _finite_float(summary["score_ratio"], "summary.score_ratio")
    trials = [
        item
        for item in payload.get("trials", [])
        if isinstance(item, dict)
        and item.get("outcome") in {"passed", "failed"}
    ]
    score = sum(
        _finite_float(item.get("score", 0.0) or 0.0, "trial.score")
        for item in trials
    )
    maximum = sum(
        _finite_float(item.get("max_score", 0.0) or 0.0, "trial.max_score")
        for item in trials
    )
    return score / maximum if maximum else 0.0


def _comparison_trials(
    payload: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in payload.get("trials", []):
        if not isinstance(raw, dict):
            continue
        case_ref = str(
            raw.get("case_ref")
            or ":".join(
                value
                for value in (
                    str(raw.get("suite_id") or ""),
                    str(raw.get("case_id") or ""),
                )
                if value
            )
        )
        if not case_ref:
            continue
        target_id = str(raw.get("target_id") or "")
        grouped.setdefault((case_ref, target_id), []).append(raw)

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for key, trials in grouped.items():
        outcome = _aggregate_trial_outcome(trials)
        scores = [
            (
                _finite_float(item.get("score", 0.0) or 0.0, "trial.score"),
                _finite_float(
                    item.get("max_score", 0.0) or 0.0,
                    "trial.max_score",
                ),
            )
            for item in trials
            if item.get("outcome") in {"passed", "failed"}
        ]
        score = sum(value for value, _maximum in scores)
        maximum = sum(maximum for _value, maximum in scores)
        result[key] = {
            "case_ref": key[0],
            "case_id": str(trials[0].get("case_id") or ""),
            "target_id": key[1],
            "outcome": outcome,
            "score_ratio": score / maximum if maximum else 0.0,
        }
    return result


def _finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    return result


def _require_finite_numbers(value: Any, path: str = "result") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must contain only finite numbers")
    if isinstance(value, dict):
        for key, item in value.items():
            _require_finite_numbers(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_finite_numbers(item, f"{path}[{index}]")


def _aggregate_trial_outcome(trials: list[dict[str, Any]]) -> str:
    outcomes = [str(item.get("outcome") or "error") for item in trials]
    if any(outcome == "error" for outcome in outcomes):
        return "error"
    if any(outcome == "failed" for outcome in outcomes):
        return "failed"
    if outcomes and all(outcome == "passed" for outcome in outcomes):
        return "passed"
    return "skipped"
