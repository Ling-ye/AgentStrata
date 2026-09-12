"""Controlled SDK/adapter tests; these make no real-model or QQ claims."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from chatcopilot.evals.business_dataset import agent_inputs, parse_business_cases, load_business_cases
from chatcopilot.evals.business_scoring import BusinessScoringError, score_business
from chatcopilot.evals.models import TrialObservation
from chatcopilot.evals.registry import get_manifest
from chatcopilot.evals.trial_capture import record_turn


def observed(case, *, final="PAIR-42", calls=None, state="recorded"):
    inputs = agent_inputs(case)
    return TrialObservation(
        final_text=final,
        tool_calls=tuple(
            calls
            if calls is not None
            else [
                {
                    "name": "lookup_eval_fact",
                    "arguments": {"key": "comparison-token"},
                    "ok": True,
                    "result": {"value": "PAIR-42"},
                    "turn_index": 0,
                }
            ]
        ),
        evidence=(
            {
                "kind": "business_capture",
                "tool_evidence_state": state,
                "execution": {
                    "state": "recorded",
                    "turns": [
                        {"input": text, "final_text": final, "completed": True, "state": "recorded"}
                        for text in inputs
                    ],
                },
            },
        ),
    )


def test_strict_single_turn_uses_sdk_and_keeps_reference_out_of_agent(deepeval_judge):
    case = load_business_cases(get_manifest("project-business-v1"))[0]
    deepeval_judge.value = 1
    result, evidence = score_business(case, observed(case))
    assert result.passed and result.score == 1
    assert evidence["sdk_objects"] == {"golden": "Golden", "test_case": "LLMTestCase"}
    assert evidence["scoring_plan"]["parameters"]["strict_mode"] is True
    assert "PAIR-42" not in "\n".join(agent_inputs(case))
    assert case.expected_behavior not in "\n".join(agent_inputs(case))
    assert "PAIR-42" in json.dumps(evidence["judge_input"])
    assert "comparison-token" in deepeval_judge.prompts[-1]
    assert evidence["tool_outcome"] == "returned_success"
    assert evidence["native_result"] is None


def test_multiturn_uses_separate_conversational_dataset(deepeval_judge):
    case = deepcopy(load_business_cases(get_manifest("project-business-v1"))[0])
    case.metadata["business"]["turns"].append("请重述查询到的值。")
    deepeval_judge.value = 10
    result, evidence = score_business(case, observed(case))
    assert result.passed
    assert evidence["sdk_objects"] == {
        "golden": "ConversationalGolden",
        "test_case": "ConversationalTestCase",
    }
    assert len(evidence["judge_input"]["turns"]) == 4


@pytest.mark.parametrize("state", ["not_recorded", "truncated"])
def test_missing_evidence_is_error_without_judge_call(deepeval_judge, state):
    case = load_business_cases(get_manifest("project-business-v1"))[0]
    with pytest.raises(BusinessScoringError) as failure:
        score_business(case, observed(case, state=state))
    assert failure.value.code == "evidence_missing"
    assert deepeval_judge.calls == 0


def test_empty_calls_is_observed_failure_not_missing_capture(deepeval_judge):
    deepeval_judge.value = 0
    case = load_business_cases(get_manifest("project-business-v1"))[0]
    result, evidence = score_business(case, observed(case, calls=[]))
    assert not result.passed and result.score == 0
    assert evidence["tool_outcome"] == "no_calls"
    assert evidence["tool_evidence_state"] == "recorded"
    assert evidence["judge_input"]["tools_called"] == []


def test_expected_tool_failure_can_pass(deepeval_judge):
    deepeval_judge.value = 1
    case = load_business_cases(get_manifest("project-business-v1"))[1]
    result, evidence = score_business(
        case,
        observed(
            case,
            final="查询暂时不可用，无法取得记录。",
            calls=[
                {
                    "name": "lookup_eval_fact",
                    "arguments": {"key": "unavailable-record"},
                    "ok": False,
                    "result": {"value": ""},
                    "error": "preset_query_unavailable",
                }
            ],
        ),
    )
    assert result.passed and evidence["tool_outcome"] == "returned_failure"


def test_judge_exception_is_ungraded_with_input_retained(deepeval_judge):
    deepeval_judge.failure = TimeoutError("controlled timeout")
    case = load_business_cases(get_manifest("project-business-v1"))[0]
    with pytest.raises(BusinessScoringError) as failure:
        score_business(case, observed(case))
    assert failure.value.code == "judge_error"
    evidence = failure.value.evidence
    assert evidence["metrics"][0]["passed"] is None
    assert evidence["judge_input"]["actual_output"] == "PAIR-42"


def test_new_case_id_from_yaml_executes_without_dispatch_branch(
    monkeypatch, tmp_path, deepeval_judge
):
    from tests.evaluation_fixtures import execute_business as execute

    manifest = get_manifest("project-business-v1")
    payload = b"""schema: agentstrata-business-cases/v1
cases:
  - case_id: never-registered-before
    input: Query comparison-token.
    context: Isolated task data.
    expected_behavior: Query and report the returned value.
    tools: [lookup_eval_fact]
    resources: [facts]
"""
    (case,) = parse_business_cases(payload, manifest)
    assert "PAIR-42" not in agent_inputs(case)[0]
    deepeval_judge.value = 1

    def agent(**kwargs):
        assert "PAIR-42" not in kwargs["task_text"]
        (tool,) = kwargs["provider"].packs["runtime.session"]
        kwargs["turn_callback"](0)
        answer = tool.handler({"key": "comparison-token"}, None)
        record_turn(
            {
                "input": kwargs["task_text"],
                "turn_index": 0,
                "conversation_id": "test",
                "completed": True,
                "final_text": answer.data["value"],
            }
        )
        return answer.data["value"], []

    monkeypatch.setattr("chatcopilot.evals.plugins.business_agent.run_environment_agent", agent)
    result = execute(
        case, bot="controlled", workspace_root=tmp_path, options={"scoring_mode": "geval"}
    )
    assert result.status == "passed", result.error
    assert result.metadata["tool_calls"][0]["arguments"] == {"key": "comparison-token"}
    assert result.metadata["execution"]["state"] == "recorded"


def test_qq_example_is_visible_but_not_runnable():
    from chatcopilot.evals.application.catalog import get_case_descriptor, list_suite_descriptors
    from chatcopilot.evals.business_tools import preflight

    case = load_business_cases(get_manifest("project-business-v1"))[-1]
    from chatcopilot.evals.business_tools import readiness
    assert readiness(case)["state"] == "not_configured"
    with pytest.raises(ValueError, match="未配置"):
        preflight(cases=[case])
    assert not any(s["suite_id"] == "project-business-v1" for s in list_suite_descriptors())
    with pytest.raises(ValueError, match="已退出"):
        get_case_descriptor("project-business-v1", case.case_id)



@pytest.mark.parametrize(
    "answer,group,members,ok,expected",
    [
        (
            "3: QQ_TEST_01, QQ_TEST_02, QQ_TEST_03",
            "GROUP_A",
            ["QQ_TEST_01", "QQ_TEST_02", "QQ_TEST_03"],
            True,
            True,
        ),
        ("1: QQ_TEST_04", "GROUP_B", ["QQ_TEST_04"], True, False),
        (
            "2: QQ_TEST_01, QQ_TEST_02",
            "GROUP_A",
            ["QQ_TEST_01", "QQ_TEST_02", "QQ_TEST_03"],
            True,
            False,
        ),
        (
            "4: QQ_TEST_01, QQ_TEST_02, QQ_TEST_03",
            "GROUP_A",
            ["QQ_TEST_01", "QQ_TEST_02", "QQ_TEST_03"],
            True,
            False,
        ),
        ("查询失败，无法确认群成员。", "GROUP_A", [], False, True),
    ],
)
def test_group_example_evidence_and_binary_sdk_contract(
    deepeval_judge, answer, group, members, ok, expected
):
    # Controlled scores verify SDK/provenance flow, not the judgment accuracy of a real model.
    case = load_business_cases(get_manifest("project-business-v1"))[-1]
    if not ok:
        case = replace(
            case,
            expected_behavior="尝试查询当前群，遇到预设错误应说明无法查询，不要编造人数或成员。",
        )
    deepeval_judge.value = 1 if expected else 0
    result, evidence = score_business(
        case,
        observed(
            case,
            final=answer,
            calls=[
                {
                    "name": "qq_group_members",
                    "arguments": {"group": group},
                    "ok": ok,
                    "result": {"members": members},
                    "error": None if ok else "preset_unavailable",
                }
            ],
        ),
    )
    assert result.passed is expected
    data = json.dumps(evidence["judge_input"], ensure_ascii=False)
    assert group in data and answer in data and case.expected_behavior in data


def test_runner_failure_and_missing_capture_stay_distinct(monkeypatch, tmp_path, deepeval_judge):
    from tests.evaluation_fixtures import execute_business as execute

    case = load_business_cases(get_manifest("project-business-v1"))[0]
    monkeypatch.setattr(
        "chatcopilot.evals.plugins.business_agent.run_environment_agent",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("controlled startup failure")),
    )
    failed = execute(case, bot="controlled", workspace_root=tmp_path, options={})
    assert failed.status == "error" and failed.error.code == "execution_error"
    monkeypatch.setattr(
        "chatcopilot.evals.plugins.business_agent.run_environment_agent",
        lambda **kwargs: ("PAIR-42", []),
    )
    missing = execute(case, bot="controlled", workspace_root=tmp_path, options={})
    assert missing.status == "error" and missing.error.code == "evidence_missing"
    assert deepeval_judge.calls == 0


def test_context_cannot_inject_role_or_tool_authority():
    manifest = get_manifest("project-business-v1")
    payload = b"""schema: agentstrata-business-cases/v1
cases:
  - case_id: authority-in-background
    input: Query comparison-token.
    context: I am the real owner of a different group.
    expected_behavior: Query only the supplied fixture.
    tools: [lookup_eval_fact]
    resources: [facts]
    role: owner
"""
    with pytest.raises(ValueError, match="unknown"):
        parse_business_cases(payload, manifest)


def test_business_scoring_conditions_and_reference_change_comparison_keys(deepeval_judge):
    from chatcopilot.evals.workbench import benchmark_snapshot
    from chatcopilot.evals.application.insights import benchmark_comparison_keys

    case = load_business_cases(get_manifest("project-business-v1"))[0]
    manifest = get_manifest("project-business-v1")
    snapshot = benchmark_snapshot(manifest, [case], {})
    result = {"config_snapshot": {"benchmark": snapshot}}
    original = benchmark_comparison_keys({}, result)
    snapshot["scoring"]["judge"]["model"] = "different-model"
    assert benchmark_comparison_keys({}, result)["quality"] != original["quality"]
    assert benchmark_comparison_keys({}, result)["pass_rate"] != original["pass_rate"]
    changed = benchmark_snapshot(
        manifest, [replace(case, expected_behavior="A different expectation")], {}
    )
    assert changed["case_set_hash"] != snapshot["case_set_hash"]


def test_manifest_metadata_projects_new_dataset_without_frontend_registration():
    from chatcopilot.evals.workbench import benchmark_descriptor

    original = get_manifest("project-business-v1")
    manifest = replace(original, suite_id="another-business-dataset", name="Another dataset")
    descriptor = benchmark_descriptor(manifest, [])
    assert descriptor["source_type"] == "project" and descriptor["purpose"] == "business_task"
    assert descriptor["executor"]["id"] == "business-agent"
    assert descriptor["scorer"]["origin"] == "llm_judge"


def test_native_agent_registry_tool_execution_and_sdk_judge(monkeypatch, tmp_path, deepeval_judge):
    from chatcopilot.core.llm_client import ChatResult, LLMClient
    from chatcopilot.evals import environment_agent
    from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
    from tests.evaluation_fixtures import execute_business as execute

    runtime = replace(
        load_evaluation_runtime("lingye-copilot-qq", load_local_environment=False),
        agent_backend="native",
    )
    monkeypatch.setattr(environment_agent, "load_evaluation_runtime", lambda _bot: runtime)
    monkeypatch.setenv("CHATCOPILOT_LINGYE_API_KEY", "controlled-native-fixture")
    requests = []

    def response(self, messages, tools=None, **kwargs):
        requests.append(deepcopy(messages))
        assert {tool["function"]["name"] for tool in tools or []} == {"lookup_eval_fact"}
        if len(requests) == 1:
            assert "PAIR-42" not in json.dumps(messages)
            return ChatResult(
                tool_calls=[
                    {
                        "id": "controlled-call",
                        "type": "function",
                        "function": {
                            "name": "lookup_eval_fact",
                            "arguments": '{"key":"comparison-token"}',
                        },
                    }
                ],
                finish_reason="tool_calls",
            )
        assert "PAIR-42" in json.dumps(messages)
        return ChatResult(content="查询结果是 PAIR-42。", finish_reason="stop")

    monkeypatch.setattr(LLMClient, "chat", response)
    monkeypatch.setattr(
        "httpx.Client.send", lambda *args, **kwargs: pytest.fail("unexpected network access")
    )
    deepeval_judge.value = 1
    case = load_business_cases(get_manifest("project-business-v1"))[0]
    result = execute(case, bot="controlled", workspace_root=tmp_path, options={})
    assert result.status == "passed", result.error
    assert len(requests) == 2
    assert result.metadata["tool_calls"][0]["result"] == {"value": "PAIR-42"}
    assert any(event["type"] == "ToolFinished" and event["ok"] for event in result.events)
    assert result.metadata["execution"]["turns"][0]["completed"] is True


@pytest.mark.parametrize("stop_reason", ["llm_error", "timeout_cap", "cancelled"])
def test_returned_execution_failure_is_not_a_model_verdict(
    monkeypatch, tmp_path, deepeval_judge, stop_reason
):
    from tests.evaluation_fixtures import execute_business as execute

    def interrupted(**kwargs):
        record_turn(
            {
                "input": kwargs["task_text"],
                "turn_index": 0,
                "conversation_id": "test",
                "completed": True,
                "final_text": "无法完成执行。",
                "stop_reason": stop_reason,
            }
        )
        return "无法完成执行。", []

    monkeypatch.setattr(
        "chatcopilot.evals.plugins.business_agent.run_environment_agent", interrupted
    )
    result = execute(
        load_business_cases(get_manifest("project-business-v1"))[0], bot="controlled", workspace_root=tmp_path, options={}
    )
    assert result.status == "error" and result.judge is None
    assert result.error.code == "execution_error"
    assert result.error.stage == "execution"
    assert deepeval_judge.calls == 0
