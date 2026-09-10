"""SDK boundary: keep single-turn and conversational datasets separate."""

from __future__ import annotations

import json
from typing import Any

from chatcopilot.evals.business_dataset import reference_material
from chatcopilot.evals.models import EvalCase, TrialObservation


def to_deepeval(case: EvalCase, observation: TrialObservation) -> tuple[Any, Any]:
    # Imports stay inside the Trial's local SDK guard.
    from deepeval.dataset import Golden, ConversationalGolden, EvaluationDataset
    from deepeval.test_case import LLMTestCase, ConversationalTestCase, Turn, ToolCall

    context = [
        json.dumps(
            {
                "task_background": case.context,
                "scorer_reference": reference_material(case),
                "visible_tool_evidence": list(observation.tool_calls),
            },
            ensure_ascii=False,
        )
    ]
    turns = business_capture(observation).get("execution", {}).get("turns", [])
    tools = [
        ToolCall(
            name=str(call["name"]),
            input_parameters=call.get("arguments", {}),
            output={"ok": call.get("ok"), "result": call.get("result"), "error": call.get("error")},
        )
        for call in observation.tool_calls
    ]
    if len(turns) > 1:
        golden = ConversationalGolden(
            scenario=case.input, expected_outcome=case.expected_behavior, context=context
        )
        transcript = []
        for index, turn in enumerate(turns):
            transcript.extend(
                [
                    Turn(role="user", content=turn["input"]),
                    Turn(
                        role="assistant",
                        content=turn["final_text"],
                        tools_called=[
                            tool
                            for tool, call in zip(tools, observation.tool_calls)
                            if call.get("turn_index") == index
                        ],
                    ),
                ]
            )
        test = ConversationalTestCase(
            scenario=golden.scenario,
            expected_outcome=golden.expected_outcome,
            context=golden.context,
            metadata={"scorer_context": context},
            turns=transcript,
        )
    else:
        golden = Golden(input=case.input, expected_output=case.expected_behavior, context=context)
        test = LLMTestCase(
            input=golden.input,
            actual_output=observation.final_text,
            expected_output=golden.expected_output,
            context=golden.context,
            tools_called=tools,
        )
    dataset = EvaluationDataset(goldens=[golden])
    dataset.add_test_case(test)
    return dataset, test


def business_capture(observation: TrialObservation) -> dict[str, Any]:
    records = [item for item in observation.evidence if item.get("kind") == "business_capture"]
    return records[0] if len(records) == 1 else {}
