"""Run native benchmark scoring and independent semantic metrics in DeepEval."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

from chatcopilot.evals import deepeval_engine as engine
from chatcopilot.evals.deepeval_engine import JudgeConfig, _local_sdk
from chatcopilot.evals.models import EvalCase, JudgeResult, to_jsonable
from chatcopilot.evals.registry import get_manifest
from chatcopilot.evals.workbench import RUBRICS, SCORING_VERSION, scoring_mode


def score_benchmark(
    suite_id: str,
    case: EvalCase,
    final_text: str,
    native_scorer: Callable[[], JudgeResult],
    *,
    options: Mapping[str, Any],
    llm_judge: bool = False,
    tool_calls: list[dict[str, Any]] | None = None,
    judge_model: Any = None,
) -> tuple[JudgeResult, dict[str, Any]]:
    mode = scoring_mode(get_manifest(suite_id), options, llm_judge=llm_judge)
    rubric = RUBRICS[str(options.get("quality_rubric", "evidence"))]
    model = judge_model
    native: JudgeResult | None = None
    metrics: list[dict[str, Any]] = []
    started = time.monotonic()
    with _local_sdk():
        from deepeval.evaluate import evaluate
        from deepeval.evaluate.configs import AsyncConfig, CacheConfig, DisplayConfig, ErrorConfig
        from deepeval.metrics import BaseMetric, GEval
        from deepeval.test_case import LLMTestCase, SingleTurnParams

        class NativeMetric(BaseMetric):
            threshold = 1.0
            async_mode = False

            @property
            def __name__(self) -> str:
                return "基准原生评分"

            def measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
                nonlocal native
                native = native_scorer()
                self.score = native.score / native.max_score if native.max_score else 0.0
                self.success = native.passed
                self.reason = "; ".join(native.reasons)
                return self.score

            async def a_measure(self, test_case: Any, *args: Any, **kwargs: Any) -> float:
                return self.measure(test_case)

            def is_successful(self) -> bool:
                return self.success is True

        test = LLMTestCase(
            input=case.input,
            actual_output=final_text or json.dumps(tool_calls or [], ensure_ascii=False),
            expected_output=str(case.metadata.get("answer") or case.expected_behavior),
            context=[json.dumps(tool_calls, ensure_ascii=False)] if tool_calls else None,
        )

        def measure(metric: Any, kind: str) -> None:
            result = evaluate(
                test_cases=[test], metrics=[metric],
                async_config=AsyncConfig(run_async=False),
                cache_config=CacheConfig(write_cache=False, use_cache=False),
                display_config=DisplayConfig(show_indicator=False, print_results=False, inspect_after_run=False),
                error_config=ErrorConfig(ignore_errors=True, skip_on_missing_params=False),
            )
            if len(result.test_results) != 1 or len(result.test_results[0].metrics_data or []) != 1:
                raise ValueError("DeepEval returned incomplete benchmark scoring")
            measured = result.test_results[0].metrics_data
            assert measured is not None
            item = measured[0]
            metrics.append({"name": item.name, "kind": kind, "score": item.score,
                            "threshold": item.threshold, "passed": item.success,
                            "reason": item.reason, "error": item.error})

        if mode != "geval":
            measure(NativeMetric(), "deterministic")
            if native is None or metrics[-1].get("error"):
                raise ValueError("native benchmark scoring failed")
        if mode != "native":
            try:
                model = model if model is not None else engine._model(JudgeConfig.from_environment())
                measure(GEval(
                    name=str(rubric["name"]), evaluation_steps=list(rubric["steps"]),
                    evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT,
                                       SingleTurnParams.EXPECTED_OUTPUT, *([SingleTurnParams.CONTEXT] if tool_calls else [])],
                    model=model, threshold=0.7, async_mode=False,
                ), "quality")
            except Exception as exc:  # preserve the independently measured native result
                metrics.append({"name": rubric["name"], "kind": "quality", "score": None,
                                "threshold": 0.7, "passed": None, "reason": "", "error": str(exc)})
            finally:
                if judge_model is None and model is not None:
                    model.model.close()
        evidence = {
            "version": SCORING_VERSION, "mode": mode,
            "native_result": to_jsonable(native) if native is not None else None,
            "metrics": metrics, "quality_applicable": mode != "native",
            "quality_reason": "仅原生评分" if mode == "native" else "",
            "judge_usage": getattr(model, "usage", {}),
            "judge_calls": getattr(model, "calls", 0),
            "judging_seconds": time.monotonic() - started,
        }
        if native is not None:
            return native, evidence
        quality = metrics[-1]
        if quality.get("error"):
            raise ValueError(f"GEval scoring failed: {quality['error']}")
        value = float(quality["score"])
        return JudgeResult(score=value, max_score=1.0, passed=quality["passed"] is True,
                           reasons=(str(quality.get("reason") or ""),)), evidence
