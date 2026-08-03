from chatcopilot.middleware.runtime.task_forecast import (
    forecast_llm_usage,
    forecast_task_usage,
    median_usage,
    normalize_usage,
)


def _call_task(
    index: int,
    *,
    model: str = "model-a",
    context: str = "sliding_window",
    role: str = "main",
) -> dict:
    return {
        "schema_version": 2,
        "status": "succeeded",
        "finished_at": index,
        "primary_model": model,
        "context_kind": context,
        "usage_totals": {
            "prompt_tokens": 1000 + index,
            "completion_tokens": 200 + index,
            "total_tokens": 1200 + index * 2,
            "cached_tokens": 300 + index,
        },
        "llm_calls": [
            {
                "model": model,
                "context_kind": context,
                "role": role,
                "raw_input_estimated_tokens": 500,
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100 + index,
                    "total_tokens": 1100 + index,
                    "cached_tokens": 200 + index,
                },
            }
        ],
    }


def test_llm_forecast_changes_at_twenty_samples_and_calibrates_input() -> None:
    history = [_call_task(index) for index in range(20)]

    cold = forecast_llm_usage(
        history[:19],
        model="model-a",
        context_kind="sliding_window",
        role="main",
        rough_input_tokens=600,
    )
    ready = forecast_llm_usage(
        history,
        model="model-a",
        context_kind="sliding_window",
        role="main",
        rough_input_tokens=600,
    )

    assert cold["status"] == "rough"
    assert cold["sample_count"] == 19
    assert ready["status"] == "ready"
    assert ready["sample_count"] == 20
    assert ready["calibration_ratio"] == 2.0
    assert ready["usage"]["prompt_tokens"] == 1200
    assert ready["usage"]["completion_tokens"] == 109


def test_forecast_isolates_model_context_and_role() -> None:
    history = [_call_task(index) for index in range(20)]
    history += [_call_task(index, model="model-b") for index in range(20)]

    mismatched = forecast_llm_usage(
        history,
        model="model-a",
        context_kind="full",
        role="main",
        rough_input_tokens=100,
    )
    subagent = forecast_llm_usage(
        history,
        model="model-a",
        context_kind="sliding_window",
        role="subagent",
        rough_input_tokens=100,
    )

    assert mismatched["sample_count"] == 0
    assert subagent["sample_count"] == 0


def test_task_forecast_threshold_and_component_median() -> None:
    history = [_call_task(index) for index in range(20)]

    insufficient = forecast_task_usage(
        history[:19],
        model="model-a",
        context_kind="sliding_window",
    )
    ready = forecast_task_usage(
        history,
        model="model-a",
        context_kind="sliding_window",
    )

    assert insufficient["status"] == "insufficient"
    assert insufficient["baseline"] is None
    assert ready["status"] == "ready"
    assert ready["baseline"]["prompt_tokens"] == 1009
    assert ready["baseline"]["completion_tokens"] == 209


def test_cache_is_an_input_subset_and_not_added_to_total() -> None:
    usage = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 150,
            "cache_read_tokens": 80,
        }
    )
    combined = median_usage([usage, usage])

    assert usage["cached_tokens"] == 100
    assert usage["non_cached_input_tokens"] == 0
    assert usage["total_tokens"] == 120
    assert combined["total_tokens"] == 120
