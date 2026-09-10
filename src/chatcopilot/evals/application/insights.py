"""Read-only result statistics and creation-time source identity for Evaluation."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

_OUTCOMES = ("passed", "failed", "error", "skipped")


def capture_source_revision(repository_root: Path) -> dict[str, Any]:
    revision: dict[str, Any] = {
        "status": "unavailable",
        "commit": None,
        "dirty": None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(repository_root)]
    try:
        head = subprocess.run(
            [*command, "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False, env=environment,
        )
        commit = head.stdout.strip()
        if head.returncode or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            return revision
        revision.update(commit=commit, status="partial")
        # Only presence is retained; filenames and diff content must not enter artifacts.
        with tempfile.TemporaryFile() as output:
            status = subprocess.run(
                [*command, "status", "--porcelain=v1", "--untracked-files=normal"],
                stdout=output, stderr=subprocess.DEVNULL, timeout=3, check=False, env=environment,
            )
            if status.returncode == 0:
                output.seek(0)
                revision.update(dirty=bool(output.read(1)), status="recorded")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return revision


def source_revision(request: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping(request.get("source_revision"))
    return {
        "status": value.get("status", "not_recorded"),
        "commit": value.get("commit"),
        "dirty": value.get("dirty"),
        "captured_at": value.get("captured_at"),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _trial_counts(trials: list[Any]) -> tuple[dict[str, int], bool]:
    counts = dict.fromkeys(_OUTCOMES, 0)
    identities: set[tuple[str, str, int]] = set()
    trial_ids: set[str] = set()
    valid = True
    for value in trials:
        trial = _mapping(value)
        outcome = trial.get("outcome")
        trial_id = trial.get("trial_id")
        case_ref = trial.get("case_ref") or trial.get("case_id")
        target = trial.get("target_id")
        attempt = trial.get("attempt")
        if not all(isinstance(item, str) and item for item in (trial_id, case_ref, target)):
            valid = False
        if type(attempt) is not int or attempt < 1:
            valid = False
        else:
            identity = (str(case_ref), str(target), attempt)
            if identity in identities:
                valid = False
            identities.add(identity)
        if isinstance(trial_id, str):
            if trial_id in trial_ids:
                valid = False
            trial_ids.add(trial_id)
        if isinstance(outcome, str) and outcome in counts:
            counts[outcome] += 1
        else:
            valid = False
    return counts, valid


def _series(request: Mapping[str, Any], result: Mapping[str, Any]) -> tuple[str | None, str]:
    if request.get("kind", result.get("kind")) != "suite":
        return None, "unsupported_kind"
    snapshot = _mapping(result.get("config_snapshot"))
    definition = dict(_mapping(snapshot.get("definition_snapshot")))
    targets = result.get("targets")
    cases = result.get("selected_cases")
    repetitions = request.get("repetitions", result.get("repetitions"))
    seed = request.get("seed", result.get("seed"))
    budget = request.get("max_wall_seconds", result.get("max_wall_seconds"))
    if not (
        snapshot.get("case_hash") and snapshot.get("judge")
        and definition.get("manifest") and definition.get("protocols")
        and definition.get("execution_implementations")
        and isinstance(targets, list) and targets
        and isinstance(cases, list) and cases
        and type(repetitions) is int and repetitions > 0
        and type(seed) is int
        and type(budget) in (int, float) and math.isfinite(budget) and budget >= 0
    ):
        return None, "missing_definition"
    lanes = []
    for target in targets:
        lane = _mapping(target)
        model_optional = (request.get("suite_id", result.get("suite")) == "agentstrata-qq-message-flow-v1"
                          and lane.get("executor") == "qq_message_flow")
        if not all(lane.get(key) for key in ("target_id", "executor", "backend")) or (not model_optional and not lane.get("model")):
            return None, "missing_target"
        lanes.append({key: lane.get(key, "") for key in (
            "target_id", "executor", "backend", "model", "reasoning_effort",
        )})
    # Retained as descriptive comparison identity; UI grouping is selected explicitly.
    for key in ("target_fingerprint", "environment_identity", "base_fingerprint"):
        definition.pop(key, None)
    material = {
        "version": 1,
        "bot_id": request.get("bot_id"),
        "suite": request.get("suite_id", result.get("suite")),
        "cases": cases,
        "case_hash": snapshot["case_hash"],
        "judge": snapshot["judge"],
        "definition": definition,
        "targets": sorted(lanes, key=lambda lane: str(lane["target_id"])),
        "repetitions": repetitions,
        "seed": seed,
        "max_wall_seconds": budget,
        "options": request.get("options", {}),
        "llm_judge": request.get("llm_judge", False),
    }
    return _digest(material), ""


def result_insights(
    request: Mapping[str, Any], result: Mapping[str, Any], *, status: str, planned: int,
) -> dict[str, Any]:
    raw_trials = result.get("trials")
    trials = raw_trials if isinstance(raw_trials, list) else []
    raw_targets = result.get("targets")
    targets = raw_targets if isinstance(raw_targets, list) else []
    summary = _mapping(result.get("summary"))
    counts, valid = _trial_counts(trials)
    if request.get("evaluation_id"):
        valid = valid and all(
            _mapping(trial).get("evaluation_id") == request["evaluation_id"] for trial in trials
        )
    source = "trials" if trials else "not_recorded"
    if not trials:
        outcomes = _mapping(summary.get("outcomes"))
        values = {key: _count(outcomes.get(key, summary.get("errors" if key == "error" else key)))
                  for key in _OUTCOMES}
        if all(value is not None for value in values.values()) and summary:
            counts = {key: int(value) for key, value in values.items() if value is not None}
            source = "summary"
    observed = sum(counts.values()) if source != "not_recorded" else None
    complete = source == "trials" and valid and observed == planned and planned > 0
    selected = result.get("selected_cases")
    repetitions = request.get("repetitions", result.get("repetitions"))
    if complete and isinstance(selected, list) and targets and type(repetitions) is int:
        expected = {(case, _mapping(target).get("target_id"), attempt)
                    for case in selected for target in targets for attempt in range(1, repetitions + 1)}
        actual = {(_mapping(trial).get("case_ref"), _mapping(trial).get("target_id"), _mapping(trial).get("attempt"))
                  for trial in trials}
        complete = actual == expected
    series_key, reason = _series(request, result)
    if status != "completed":
        reason = "not_completed"
    elif request.get("dry_run") is True or any(
        _mapping(item).get("executor") == "dry_run" for item in targets
    ):
        reason = "dry_run"
    elif not complete:
        reason = "incomplete_trials" if valid else "invalid_trials"
    snapshot = _mapping(result.get("config_snapshot"))
    config_fingerprints = {
        str(_mapping(target).get("target_id")): _mapping(target).get("config_fingerprint")
        for target in targets
        if _mapping(target).get("config_fingerprint")
    }
    configuration = {
        "bot_spec": request.get("bot_spec_sha256"),
        "targets": config_fingerprints,
        "environment": snapshot.get("environment_fingerprint"),
    }
    capabilities: dict[str, dict[str, int]] = {}
    for value in trials:
        trial = _mapping(value)
        dimension = str(trial.get("dimension") or "未分组")
        outcome = trial.get("outcome")
        if isinstance(outcome, str) and outcome in _OUTCOMES:
            group = capabilities.setdefault(dimension, dict.fromkeys(_OUTCOMES, 0))
            group[outcome] += 1
    return {
        "counts": counts if source != "not_recorded" else None,
        "observed": observed,
        "planned": planned,
        "pass_rate": counts["passed"] / observed if observed and valid else None,
        "complete": complete,
        "source": source,
        "verdict": summary.get("verdict"),
        "capabilities": capabilities,
        "series_key": series_key,
        "trend_eligible": not reason,
        "exclusion_reason": reason,
        "configuration_fingerprint": _digest(configuration) if any(configuration.values()) else None,
        "benchmark_fingerprint": snapshot.get("case_hash"),
        "quality": quality_summary(trials),
        "comparison_keys": benchmark_comparison_keys(request, result),
        "targets": target_summaries(result, request),
    }


def benchmark_comparison_keys(request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, str]:
    snapshot = _mapping(result.get("config_snapshot"))
    benchmark = _mapping(request.get("benchmark") or snapshot.get("benchmark"))
    if benchmark.get("schema") != "evaluation-workbench/v1" or not benchmark.get("case_set_hash"):
        return {}
    definition = _mapping(snapshot.get("definition_snapshot"))
    implementations = _mapping(_mapping(definition.get("execution_implementations")).get("modules"))
    scoring = _mapping(benchmark.get("scoring"))
    material = {key: benchmark.get(key) for key in ("suite_id", "adapter_version", "case_set_hash", "environment_contract", "budget")}
    material["protocols"] = definition.get("protocols")
    material["driver"] = _mapping(definition.get("manifest")).get("driver_id")
    material["native_implementations"] = {key: value for key, value in implementations.items() if ".adapters." in key or key.endswith(("capability_verifiers", "business_verifiers", "ifeval_subset"))}
    material["native_mode"] = scoring.get("native")
    suite_id = benchmark.get("suite_id")
    if suite_id in {"swe-bench-verified", "agentbench-fc"}:
        environments = []
        for raw in result.get("trials", []):
            trial = _mapping(raw)
            evidence = _mapping(trial.get("evidence"))
            lease = _mapping(_mapping(evidence.get("execution")).get("environment"))
            identity = evidence.get("image_id") if suite_id == "swe-bench-verified" else lease.get("controller_fingerprint")
            if not isinstance(identity, str) or not identity:
                return {}
            environments.append((str(trial.get("case_id")), identity))
        if not environments:
            return {}
        material["observed_environments"] = sorted(set(environments))
    native = _digest(material)
    quality_implementations = {key: value for key, value in implementations.items()
                               if key.endswith(("deepeval_engine", "benchmark_scoring", "workbench"))}
    quality = _digest({**material, "scoring": scoring, "quality_implementations": quality_implementations})
    # Product pass/fail includes required quality, while public native results do not.
    product = benchmark.get("suite_id") == "agentstrata-capabilities-v1"
    return {"pass_rate": quality if product or scoring.get("native") is False else native,
            "quality": quality, "duration": native}


def _nonnegative_number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def quality_summary(trials: list[Any]) -> dict[str, Any]:
    values: list[float] = []
    expected = 0
    for trial in trials:
        judging = _mapping(_mapping(_mapping(trial).get("evidence")).get("judge_evidence"))
        if judging.get("quality_applicable") is not True:
            continue
        expected += 1
        metrics = judging.get("metrics", [])
        quality = [m for m in metrics if isinstance(m, Mapping) and m.get("kind") == "quality"] if isinstance(metrics, list) else []
        scores = [_nonnegative_number(m.get("score")) for m in quality if not m.get("error")]
        valid_scores = [score for score in scores if score is not None and score <= 1]
        if valid_scores and len(valid_scores) == len(quality):
            values.append(sum(valid_scores) / len(valid_scores))
    return {"score": sum(values) / len(values) if values else None, "scored": len(values), "expected": expected}


def execution_duration(trials: list[Any], *, kind: str = "agent") -> dict[str, Any]:
    """Project complete measurements and sampled lower bounds without rewriting history."""
    known: list[float] = []
    recorded = partial = 0
    for trial in trials:
        evidence = _mapping(_mapping(trial).get("evidence"))
        timing = _mapping(_mapping(evidence.get("execution")).get("timing"))
        if timing:
            value = _nonnegative_number(timing.get("seconds")) if timing.get("kind") == kind else None
            state = timing.get("state")
            if value is not None and state in {"complete", "partial", "running"}:
                known.append(value)
                if state == "complete":
                    recorded += 1
                else:
                    partial += 1
        else:
            value = _nonnegative_number(evidence.get("agent_duration_seconds" if kind == "agent" else "runtime_duration_seconds"))
            if value is None and kind == "runtime":
                # Earlier QQ observations used the Agent field for the whole driver.
                value = _nonnegative_number(evidence.get("agent_duration_seconds"))
            if value is not None:
                known.append(value)
                recorded += 1
    subtotal = _nonnegative_number(sum(known)) if known else None
    complete = bool(trials) and recorded == len(trials) and subtotal is not None
    return {"kind": kind, "total_seconds": subtotal if complete else None,
            "recorded_seconds": subtotal, "recorded": recorded, "partial": partial,
            "expected": len(trials), "complete": complete}


def target_summaries(result: Mapping[str, Any], request: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    all_trials = result.get("trials", [])
    if not isinstance(all_trials, list):
        return rows
    kind = "runtime" if request.get("suite_id", result.get("suite")) == "agentstrata-qq-message-flow-v1" else "agent"
    for target in result.get("targets", []):
        lane = _mapping(target)
        trials = [t for t in all_trials if _mapping(t).get("target_id") == lane.get("target_id")]
        counts, valid = _trial_counts(trials)
        cases = sorted({str(_mapping(t).get("case_id") or "") for t in trials})
        duration = execution_duration(trials, kind=kind)
        rows.append({
            "target_id": lane.get("target_id"), "backend": lane.get("backend"),
            "model": lane.get("model"), "reasoning_effort": lane.get("reasoning_effort", ""),
            "counts": counts, "observed": len(trials),
            "pass_rate": counts["passed"] / len(trials) if trials and valid else None,
            "quality": quality_summary(trials), "duration": duration,
            "agent_duration_seconds": duration["total_seconds"] if kind == "agent" else None,
            "case_ids": cases, "repetitions": request.get("repetitions", result.get("repetitions")),
            "cases": [{"case_id": case_id, "counts": _trial_counts(selected)[0],
                "quality": quality_summary(selected), "duration": execution_duration(selected, kind=kind),
                "agent_duration_seconds": execution_duration(selected, kind=kind)["total_seconds"] if kind == "agent" else None}
                for case_id in cases if (selected := [t for t in trials if _mapping(t).get("case_id") == case_id])],
            "scoring": _mapping(_mapping(result.get("config_snapshot")).get("definition_snapshot")).get("scoring"),
        })
    return rows


def trial_preview(trial: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _mapping(trial.get("evidence"))
    execution = _mapping(evidence.get("execution"))
    turns = execution.get("turns", [])
    first = _mapping(turns[0]) if isinstance(turns, list) and turns else {}
    judging = _mapping(evidence.get("judge_evidence"))
    return {
        **{key: trial.get(key) for key in ("trial_id", "case_id", "case_ref", "target_id", "attempt",
            "outcome", "duration_seconds", "started_at", "stop_reason", "error", "score", "max_score", "passed")},
        "final_text": str(trial.get("final_text") or "")[:400],
        "input_preview": str(first.get("input") or evidence.get("input") or "")[:400],
        "body_available": True, "capture_state": execution.get("state", "not_recorded"),
        "evidence": {"case_source": _mapping(evidence.get("case_source")), "judge_evidence": {key: judging.get(key) for key in ("quality_applicable", "quality_reason", "metrics", "error", "native_result", "mode")}},
    }
