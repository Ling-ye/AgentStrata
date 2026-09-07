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
        if not all(lane.get(key) for key in ("target_id", "executor", "backend", "model")):
            return None, "missing_target"
        lanes.append({key: lane.get(key, "") for key in (
            "target_id", "executor", "backend", "model", "reasoning_effort",
        )})
    # Runtime/configuration changes are observations; benchmark changes split the series.
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
    }
