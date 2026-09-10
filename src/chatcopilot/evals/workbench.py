"""Public benchmark metadata and immutable scoring plans for the workbench."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Any, Mapping, Sequence

from chatcopilot.evals.deepeval_engine import ENGINE_VERSION, scoring_snapshot
from chatcopilot.evals.models import EvalCase, EvalCaseDefinition, SuiteManifest

SCORING_VERSION = "benchmark-scoring/v1"
EXTERNAL_SUITES = frozenset({"bfcl", "gaia", "ifeval", "swe-bench-verified", "agentbench-fc"})
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
_BENCHMARKS = {
    "gaia": ("GAIA", "Level 1 / 2 / 3，以实际载入数据为准", "GAIA 官方答案归一化匹配", "Agent runtime"),
    "bfcl": ("BFCL", "v3 数据格式：simple / multiple / parallel / parallel_multiple / relevance；当前为部分协议校准", "调用名称与参数匹配（项目适配器）", "模型函数调用接口 · direct_llm"),
    "swe-bench-verified": ("SWE-bench Verified", "固定数据与容器测试环境", "SWE-bench 5.0.2 原生评分 · 受限容器执行", "Agent runtime · 隔离代码任务"),
    "agentbench-fc": ("AgentBench FC", "FC 环境协议：DB / OS / KG / ALFWorld / WebShop；以本地环境与题目目录为准", "环境原生 success / reward", "Agent runtime · 环境交互"),
    "ifeval": ("IFEval", "已实现的确定性指令类别", "指令约束检查（支持子集）", "Agent runtime"),
    "agentstrata-capabilities-v1": ("AgentStrata 回归", "自建能力题与固定 IFEval 子集", "执行事实与必需质量门禁", "Agent runtime · 不经过 QQ / ACP"),
    "agentstrata-qq-message-flow-v1": ("QQ 合成链路", "Legacy Relay / attestation / ACP 回归", "确定性链路断言", "合成链路 · 非真实 QQ / 当前 Gateway E2E"),
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def scoring_mode(suite_id: str, options: Mapping[str, Any], *, llm_judge: bool = False) -> str:
    mode = options.get("scoring_mode") or (
        "native_geval" if suite_id == "agentstrata-capabilities-v1" or llm_judge else "native"
    )
    supported = {"native", "native_geval"} if suite_id == "agentstrata-capabilities-v1" else {"native", "native_geval", "geval"}
    if mode not in supported:
        raise ValueError("unsupported scoring mode")
    return str(mode)


def capability_scoring(definition: EvalCaseDefinition, options: Mapping[str, Any]) -> EvalCaseDefinition:
    if scoring_mode("agentstrata-capabilities-v1", options) == "native":
        return replace(definition, quality={"enabled": False, "reason": "本次选择仅执行事实评分"})
    return definition


def scoring_plan(suite_id: str, options: Mapping[str, Any], *, llm_judge: bool = False) -> dict[str, Any]:
    mode = scoring_mode(suite_id, options, llm_judge=llm_judge)
    rubric_id = str(options.get("quality_rubric", "evidence"))
    if rubric_id not in RUBRICS:
        raise ValueError("unknown quality rubric")
    quality = mode != "native"
    snapshot = scoring_snapshot()
    return {
        "version": SCORING_VERSION,
        "framework": "DeepEval",
        "framework_version": ENGINE_VERSION,
        "mode": mode,
        "native": mode != "geval",
        "native_method": _BENCHMARKS.get(suite_id, ("", "", "Suite scorer", ""))[2],
        "quality": quality,
        "method": "LLM-as-a-Judge / G-Eval" if quality else "deterministic",
        "implementation": "GEval / ConversationalGEval" if suite_id == "agentstrata-capabilities-v1" and quality else "GEval" if quality else "BaseMetric",
        "judge": snapshot["judge"] if quality else None,
        "rubric": {"id": rubric_id, "version": SCORING_VERSION, "threshold": 0.7, **RUBRICS[rubric_id]} if quality and suite_id in EXTERNAL_SUITES else None,
        "policy_version": snapshot["policy_version"] if suite_id == "agentstrata-capabilities-v1" else SCORING_VERSION,
    }


def benchmark_descriptor(manifest: SuiteManifest, cases: Sequence[EvalCase]) -> dict[str, Any]:
    name, coverage, native, target = _BENCHMARKS.get(manifest.suite_id, (manifest.name, "", "Suite scorer", ""))
    is_qq = manifest.track == "qq_message_flow"
    return {
        "name": name,
        "framework": "AgentStrata 链路检查器" if is_qq else "DeepEval",
        "framework_version": "" if is_qq else ENGINE_VERSION,
        "adapter_version": manifest.version,
        "coverage": coverage,
        "native_method": native,
        "target_scope": target,
        "scoring_modes": ["native"] if is_qq else ["native", "native_geval"] if manifest.suite_id == "agentstrata-capabilities-v1" else ["native", "native_geval", "geval"],
        "default_scoring_mode": "native" if is_qq else "native_geval",
        "rubrics": [{"id": key, "threshold": 0.7, **value} for key, value in RUBRICS.items()] if manifest.suite_id in EXTERNAL_SUITES else [],
        "judge": None if is_qq else scoring_snapshot()["judge"],
        "case_set_hash": digest([{"id": case.case_id, "input": case.input, "metadata": case.metadata} for case in cases]),
    }


def benchmark_snapshot(manifest: SuiteManifest, cases: Sequence[EvalCase], options: Mapping[str, Any], *, llm_judge: bool = False) -> dict[str, Any]:
    descriptor = benchmark_descriptor(manifest, cases)
    return {
        "schema": "evaluation-workbench/v1",
        "suite_id": manifest.suite_id,
        "name": descriptor["name"],
        "adapter_version": manifest.version,
        "coverage": descriptor["coverage"],
        "target_scope": descriptor["target_scope"],
        "case_ids": [case.case_id for case in cases],
        "case_set_hash": descriptor["case_set_hash"],
        "scoring": scoring_plan(manifest.suite_id, options, llm_judge=llm_judge),
    }
