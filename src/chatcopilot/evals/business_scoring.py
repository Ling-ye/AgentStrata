"""Strict LLM task judgments with explicit execution and evidence failures."""

from __future__ import annotations

import time
from typing import Any

from chatcopilot.evals import deepeval_engine as engine
from chatcopilot.evals.models import EvalCase, JudgeResult, TrialObservation

from chatcopilot.evals.business_policy import (
    BUSINESS_SCORING_VERSION,
    EVALUATION_STEPS,
    business_scoring_plan,
)
from chatcopilot.evals.deepeval_mapping import business_capture, to_deepeval


class BusinessScoringError(ValueError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any]):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


def score_business(
    case: EvalCase, observation: TrialObservation
) -> tuple[JudgeResult, dict[str, Any]]:
    captured = business_capture(observation)
    plan = business_scoring_plan()
    evidence: dict[str, Any] = {
        "version": BUSINESS_SCORING_VERSION,
        "mode": "geval",
        "primary": "llm_judge",
        "quality_applicable": True,
        "scoring_plan": plan,
        "metrics": [],
        "native_result": None,
        "tool_evidence_state": captured.get("tool_evidence_state", "not_recorded"),
        "tool_outcome": "no_calls"
        if not observation.tool_calls
        else "returned_failure"
        if any(c.get("ok") is False for c in observation.tool_calls)
        else "returned_success",
    }
    turns = captured.get("execution", {}).get("turns", [])
    if observation.structured_error:
        raise BusinessScoringError("execution_error", "业务题执行异常，不能判为模型答错", evidence)
    expected_turns = len(case.metadata.get("business", {}).get("turns", [case.input]))
    if (
        evidence["tool_evidence_state"] != "recorded"
        or captured.get("execution", {}).get("state") != "recorded"
        or len(turns) != expected_turns
        or any(t.get("completed") is not True or t.get("state") != "recorded" for t in turns)
        or any(
            "name" not in c
            or "arguments" not in c
            or "result" not in c
            or type(c.get("ok")) is not bool
            for c in observation.tool_calls
        )
    ):
        raise BusinessScoringError("evidence_missing", "必需的工具或回合证据未完整采集", evidence)
    started = time.monotonic()
    model = None
    try:
        with engine._local_sdk():
            from deepeval import evaluate
            from deepeval.evaluate.configs import (
                AsyncConfig,
                CacheConfig,
                DisplayConfig,
                ErrorConfig,
            )
            from deepeval.metrics import GEval, ConversationalGEval
            from deepeval.test_case import LLMTestCase, SingleTurnParams, MultiTurnParams

            dataset, test = to_deepeval(case, observation)
            model = engine._model(engine.JudgeConfig.from_environment())
            common = {
                "name": "LLM 判定 · 业务任务完成",
                "evaluation_steps": list(EVALUATION_STEPS),
                "model": model,
                "strict_mode": True,
                "async_mode": False,
            }
            if isinstance(test, LLMTestCase):
                metric = GEval(
                    **common,
                    evaluation_params=[
                        SingleTurnParams.INPUT,
                        SingleTurnParams.ACTUAL_OUTPUT,
                        SingleTurnParams.EXPECTED_OUTPUT,
                        SingleTurnParams.CONTEXT,
                        SingleTurnParams.TOOLS_CALLED,
                    ],
                )
            else:
                metric = ConversationalGEval(
                    **common,
                    evaluation_params=[
                        MultiTurnParams.CONTENT,
                        MultiTurnParams.ROLE,
                        MultiTurnParams.SCENARIO,
                        MultiTurnParams.EXPECTED_OUTCOME,
                        MultiTurnParams.METADATA,
                        MultiTurnParams.TOOLS_CALLED,
                    ],
                )
            evidence["sdk_objects"] = {
                "golden": type(dataset.goldens[0]).__name__,
                "test_case": type(test).__name__,
            }
            evidence["judge_input"] = (
                test.model_dump(mode="json")
                if hasattr(test, "model_dump")
                else {
                    "input": test.input,
                    "actual_output": test.actual_output,
                    "expected_output": test.expected_output,
                    "context": test.context,
                    "tools_called": [
                        tool.model_dump(mode="json") for tool in test.tools_called or []
                    ],
                }
            )
            result = evaluate(
                test_cases=dataset.test_cases,
                metrics=[metric],
                async_config=AsyncConfig(run_async=False),
                cache_config=CacheConfig(write_cache=False, use_cache=False),
                display_config=DisplayConfig(
                    show_indicator=False, print_results=False, inspect_after_run=False
                ),
                error_config=ErrorConfig(ignore_errors=True, skip_on_missing_params=False),
            )
            if len(result.test_results) != 1 or len(result.test_results[0].metrics_data or []) != 1:
                raise ValueError("DeepEval returned incomplete business scoring")
            item = result.test_results[0].metrics_data[0]
            if item.error or item.score not in (0, 1) or type(item.success) is not bool:
                raise ValueError(item.error or "DeepEval strict judgment is not binary")
            evidence["metrics"] = [
                {
                    "name": item.name,
                    "kind": "quality",
                    "score": item.score,
                    "threshold": item.threshold,
                    "passed": item.success,
                    "reason": item.reason,
                    "error": None,
                }
            ]
            return JudgeResult(
                score=float(item.score),
                max_score=1,
                passed=item.success,
                reasons=(item.reason or "",),
            ), evidence
    except Exception as exc:
        evidence["metrics"] = [
            {
                "name": "LLM 判定",
                "kind": "quality",
                "score": None,
                "threshold": 1,
                "passed": None,
                "reason": "",
                "error": str(exc),
            }
        ]
        raise BusinessScoringError("judge_error", f"LLM 评分异常：{exc}", evidence) from exc
    finally:
        evidence["judging_seconds"] = time.monotonic() - started
        evidence["judge_calls"] = getattr(model, "calls", 0)
        evidence["judge_usage"] = getattr(model, "usage", {})
        if model is not None:
            model.model.close()
