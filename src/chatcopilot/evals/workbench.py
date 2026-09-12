"""Public benchmark metadata and immutable scoring plans for the workbench."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Any, Mapping, Sequence

from chatcopilot.evals.deepeval_engine import ENGINE_VERSION, scoring_snapshot
from chatcopilot.evals.models import EvalCase, EvalCaseDefinition, SuiteManifest

SCORING_VERSION = "benchmark-scoring/v1"
RUBRICS = {
    "evidence": {
        "name": "证据与回答质量",
        "steps": [
            "Assess whether the response is supported by the supplied reference and observed tool results.",
            "Check that the response addresses the requested task without unsupported claims.",
            "Do not infer execution success from the response; assess only the provided evidence.",
        ],
    },
    "task": {
        "name": "任务回应质量",
        "steps": [
            "Assess how completely the public response addresses the user's requested deliverable.",
            "Check clarity and consistency with the supplied reference and public execution evidence.",
            "Penalize unsupported completion claims; do not treat fluency as task success.",
        ],
    },
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def supported_modes(manifest: SuiteManifest) -> list[str]:
    if manifest.plugin_id == "agent-tasks":
        return ["native_geval"]
    if manifest.purpose == "business_task":
        return ["geval"]
    if manifest.track == "qq_message_flow":
        return ["native"]
    return ["native", "native_geval"]


def default_mode(manifest: SuiteManifest) -> str:
    if manifest.purpose == "business_task":
        return "geval"
    return "native_geval" if manifest.plugin_id in {"generic-agent", "agent-tasks"} else "native"


def scoring_mode(
    manifest: SuiteManifest, options: Mapping[str, Any], *, llm_judge: bool = False
) -> str:
    mode = options.get("scoring_mode") or ("native_geval" if llm_judge else default_mode(manifest))
    if mode not in supported_modes(manifest):
        raise ValueError("unsupported scoring mode")
    return str(mode)


def capability_scoring(
    definition: EvalCaseDefinition, options: Mapping[str, Any]
) -> EvalCaseDefinition:
    mode = options.get("scoring_mode", "native_geval")
    if mode not in {"native", "native_geval"}:
        raise ValueError("unsupported engineering scoring mode")
    if definition.plugin_id == "agent-tasks" and mode != "native_geval":
        raise ValueError("required task semantic criteria cannot be disabled")
    if mode == "native":
        return replace(definition, quality={"enabled": False, "reason": "本次选择仅执行事实评分"})
    return definition


def scoring_plan(
    manifest: SuiteManifest, options: Mapping[str, Any], *, llm_judge: bool = False
) -> dict[str, Any]:
    suite_id = manifest.suite_id
    mode = scoring_mode(manifest, options, llm_judge=llm_judge)
    if manifest.purpose == "business_task":
        from chatcopilot.evals.business_policy import business_scoring_plan

        return business_scoring_plan()
    rubric_id = str(options.get("quality_rubric", "evidence"))
    if rubric_id not in RUBRICS:
        raise ValueError("unknown quality rubric")
    quality = mode != "native"
    snapshot = scoring_snapshot()
    return {
        "version": SCORING_VERSION,
        "scorer": scorer_descriptor(manifest),
        "primary": "native",
        "framework": "AgentStrata 链路检查器"
        if manifest.track == "qq_message_flow"
        else "DeepEval",
        "framework_version": "" if manifest.track == "qq_message_flow" else ENGINE_VERSION,
        "mode": mode,
        "native": mode != "geval",
        "native_method": manifest.native_method,
        "quality": quality,
        "method": "LLM-as-a-Judge / G-Eval" if quality else "deterministic",
        "implementation": "GEval / ConversationalGEval"
        if suite_id == "agentstrata-capabilities-v1" and quality
        else "GEval"
        if quality
        else "BaseMetric",
        "judge": snapshot["judge"] if quality else None,
        "rubric": {
            "id": rubric_id,
            "version": SCORING_VERSION,
            "threshold": 0.7,
            **RUBRICS[rubric_id],
        }
        if quality and manifest.source_type == "public_benchmark"
        else None,
        "policy_version": snapshot["policy_version"]
        if suite_id == "agentstrata-capabilities-v1"
        else SCORING_VERSION,
    }


def benchmark_descriptor(manifest: SuiteManifest, cases: Sequence[EvalCase]) -> dict[str, Any]:
    name, coverage, native, target = (
        manifest.name,
        manifest.coverage,
        manifest.native_method,
        manifest.target_scope,
    )
    from chatcopilot.evals.business_policy import business_scoring_plan

    is_qq = manifest.track == "qq_message_flow"
    return {
        "name": name,
        **organization_descriptor(manifest),
        "framework": "AgentStrata 链路检查器" if is_qq else "DeepEval",
        "framework_version": "" if is_qq else ENGINE_VERSION,
        "adapter_version": manifest.version,
        "coverage": coverage,
        "native_method": native,
        "target_scope": target,
        "scoring_modes": supported_modes(manifest),
        "default_scoring_mode": default_mode(manifest),
        "rubrics": [{"id": key, "threshold": 0.7, **value} for key, value in RUBRICS.items()]
        if manifest.source_type == "public_benchmark"
        else [],
        "judge": None if is_qq else scoring_snapshot()["judge"],
        "default_scoring": business_scoring_plan() if manifest.purpose == "business_task" else None,
        "case_set_hash": digest(
            [
                {
                    "id": case.case_id,
                    "input": case.input,
                    "context": case.context,
                    "expected_behavior": case.expected_behavior,
                    "rubric": case.rubric,
                    "metadata": case.metadata,
                }
                for case in cases
            ]
        ),
    }


def benchmark_snapshot(
    manifest: SuiteManifest,
    cases: Sequence[EvalCase],
    options: Mapping[str, Any],
    *,
    llm_judge: bool = False,
) -> dict[str, Any]:
    descriptor = benchmark_descriptor(manifest, cases)
    return {
        "schema": "evaluation-workbench/v2",
        **organization_descriptor(manifest),
        "suite_id": manifest.suite_id,
        "name": descriptor["name"],
        "adapter_version": manifest.version,
        "coverage": descriptor["coverage"],
        "target_scope": descriptor["target_scope"],
        "case_ids": [case.case_id for case in cases],
        "case_set_hash": descriptor["case_set_hash"],
        "scoring": scoring_plan(manifest, options, llm_judge=llm_judge),
    }


def scorer_descriptor(manifest: SuiteManifest) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "name": manifest.native_method,
        "origin": manifest.scorer_origin,
        "version": manifest.scorer_version,
        "implementation": manifest.plugin_id,
    }
    if manifest.plugin_id == "ifeval":
        from importlib.metadata import version, PackageNotFoundError
        import platform

        dependencies = {"python": platform.python_version()}
        for package in ("nltk", "langdetect", "immutabledict", "absl-py"):
            try:
                dependencies[package] = version(package)
            except PackageNotFoundError:
                dependencies[package] = "unavailable"
        descriptor["dependency_versions"] = dependencies
        from chatcopilot.evals.ifeval_resources import REVISION as resource_revision, resource_dir
        from hashlib import sha256
        receipt = resource_dir() / "source.json"
        descriptor["sentence_resources"] = {"revision": resource_revision, "receipt_sha256": sha256(receipt.read_bytes()).hexdigest() if receipt.is_file() else "unavailable"}
    return descriptor


def organization_descriptor(manifest: SuiteManifest) -> dict[str, Any]:
    return {
        "subject_type": manifest.subject_type,
        "capability_tags": list(manifest.capability_tags),
        "source_type": manifest.source_type,
        "purpose": manifest.purpose,
        "data_version": manifest.data_version or manifest.version,
        "split": manifest.split,
        "executor": {
            "id": manifest.plugin_id,
            "driver": manifest.driver_id,
            "version": manifest.version,
            "target": manifest.target_scope,
        },
        "scorer": scorer_descriptor(manifest),
    }
