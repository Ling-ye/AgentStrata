"""IFEval directly evaluates a model using an independent PromptPlan."""

from chatcopilot.evals.adapters import ifeval
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION


def _execute_trial(case, *, chat_config):
    from chatcopilot.agent.context.prompt_plan import (
        PromptBuildInput,
        PromptPlanBuilder,
        render_native_prefix,
    )
    from chatcopilot.contracts.prompt import BotPromptProfile
    from chatcopilot.core.llm_client import LLMClient
    from chatcopilot.evals.trial_capture import record_turn

    plan = PromptPlanBuilder().build(
        PromptBuildInput(
            profile=BotPromptProfile(
                identity="Instruction-following evaluation assistant.",
                response_style="Follow the task requirements.",
            ),
            backend="native",
            model=chat_config.llm.model,
            role="user",
            channel_kind="private",
            skill_index=(),
            session_policy="Answer the user's instruction-following task. No tools are available.",
        )
    )
    messages = [*render_native_prefix(plan), {"role": "user", "content": case.input}]
    turn = {
        "turn_index": 0,
        "conversation_id": case.case_id,
        "input": case.input,
        "messages": messages,
        "completed": False,
    }
    record_turn(turn)
    client = LLMClient(chat_config.llm)
    try:
        response = client.chat(messages=messages, tools=None, stream=False, max_retries=0)
    finally:
        client.close()
    if getattr(response, "tool_calls", None):
        raise ValueError("IFEval model-only response requested tools")
    output = response.content or ""
    record_turn({**turn, "completed": True, "final_text": output, "stop_reason": "end_turn"})
    return {
        "final_text": output,
        "tool_calls": [],
        "usage": response.usage or {},
        "metadata": {
            "subject_type": "model",
            "agent_runtime_exercised": False,
            "model_input_messages": messages,
            "prompt_protocol": "independent_prompt_plan_with_host_policy",
            "official_prompt_equivalent": False,
        },
    }


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
