"""IFEval directly evaluates a model using an independent PromptPlan."""

from chatcopilot.evals.adapters import ifeval
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION


def _execute_trial(case, *, chat_config):
    from chatcopilot.evals.model_io import invoke

    observation = invoke(case, chat_config=chat_config, messages=[{"role": "user", "content": case.input}], tools=None)
    if observation["tool_calls"]:
        raise ValueError("IFEval model-only response requested tools")
    return observation


def _judge(case, observation):
    from chatcopilot.evals.models import JudgeResult

    results = ifeval.instruction_results(case, observation["final_text"])
    observation.setdefault("metadata", {}).update(
        instructions=results,
        ifeval_metrics={
            "prompt_strict": all(x["passed"] for x in results),
            "prompt_loose": all(x["loose_passed"] for x in results),
            "instruction_strict": sum(x["passed"] for x in results) / len(results),
            "instruction_loose": sum(x["loose_passed"] for x in results) / len(results),
        },
    )
    passed = all(x["passed"] for x in results)
    return JudgeResult(
        float(passed),
        1.0,
        passed,
        reasons=tuple(x["id"] + (": passed" if x["passed"] else ": failed") for x in results),
    )


PLUGIN = EvaluationPlugin(
    plugin_id="ifeval",
    api_version=PLUGIN_API_VERSION,
    implementation_module=__name__,
    allowed_drivers=frozenset({"direct_llm", "dry_run"}),
    load_cases=lambda context: ifeval.load_cases(),
    preflight=lambda *, cases: ifeval.preflight(cases),
    execute_trial=_execute_trial,
    judge=_judge,
)
