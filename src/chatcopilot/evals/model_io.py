"""Direct model input/output capture and read-only historical output projection."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping


def invoke(case, *, chat_config, messages: list[dict], tools: list[dict] | None) -> dict[str, Any]:
    from chatcopilot.agent.context.prompt_plan import PromptBuildInput, PromptPlanBuilder, render_native_prefix
    from chatcopilot.contracts.prompt import BotPromptProfile
    from chatcopilot.core.llm_client import LLMClient
    from chatcopilot.evals.trial_capture import record_turn

    plan = PromptPlanBuilder().build(PromptBuildInput(
        profile=BotPromptProfile(identity="Independent model evaluation assistant.", response_style="Follow the benchmark task requirements."),
        backend="native", model=chat_config.llm.model, role="user", channel_kind="private", skill_index=(),
        tool_names=tuple(t["function"]["name"] for t in tools or []),
        session_policy="Generate the requested answer or function calls. No tools are executed in this model evaluation.",
    ))
    # Benchmark system text remains task data, not host policy.
    task_messages = [{"role": "user", "content": "Benchmark context:\n" + m["content"]}
                     if m["role"] == "system" else deepcopy(m) for m in messages]
    request = {"model": chat_config.llm.model, "messages": [*render_native_prefix(plan), *task_messages], "tools": deepcopy(tools)}
    turn = {"turn_index": 0, "conversation_id": case.case_id, "input": case.input,
            "model_request": request, "completed": False}
    record_turn(turn)
    client = LLMClient(chat_config.llm)
    try:
        reply = client.chat(messages=request["messages"], tools=request["tools"], stream=False, max_retries=0)
        response = {"content": reply.content or "", "tool_calls": deepcopy(reply.tool_calls or []),
                    "finish_reason": reply.finish_reason, "usage": deepcopy(reply.usage)}
        record_turn({**turn, "model_response": response, "completed": True,
                     "final_text": response["content"], "stop_reason": "end_turn"})
    finally:
        client.close()
    return {"final_text": response["content"], "tool_calls": response["tool_calls"], "usage": reply.usage or {},
            "metadata": {"model_request": request, "model_response": response, "subject_type": "model",
                         "agent_runtime_exercised": False, "prompt_protocol": "independent_prompt_plan_with_host_policy",
                         "official_prompt_equivalent": False}}


def output_preview(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded projection, never synthesizing final_text or updating stored evidence."""
    evidence = trial.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    response = evidence.get("model_response")
    explicit = isinstance(response, dict)
    response = response if explicit else {}
    calls = response.get("tool_calls", evidence.get("tool_calls", []))
    calls = calls if isinstance(calls, list) else []
    text = response.get("content", trial.get("final_text", "")) or ""
    execution = evidence.get("execution") or {}
    if not isinstance(execution, dict):
        execution = {}
    recorded = explicit or bool(text or calls) or (execution.get("state") == "recorded"
        and "tool_calls" in evidence and any(t.get("completed") for t in execution.get("turns", []) if isinstance(t, dict)))
    summary = []
    for call in calls[:3]:
        if not isinstance(call, dict):
            continue
        function = call.get("function", call)
        if not isinstance(function, dict):
            continue
        args = function.get("arguments", {})
        summary.append({"name": str(function.get("name") or "")[:80],
                        "arguments": (args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))[:160]})
    return {"kind": "mixed" if text and calls else "tool_calls" if calls else "text" if text else "empty" if recorded else "missing",
            "text": str(text)[:400], "calls": summary, "call_count": len(calls),
            "finish_reason": response.get("finish_reason"), "truncated": execution.get("state") == "truncated"}
