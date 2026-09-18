"""Counterexamples for missing premises and alternative observable task paths."""

from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import shlex
import subprocess

import pytest

from chatcopilot.evals import deepeval_engine as engine
from chatcopilot.evals.agent_tasks.evidence import TaskFileEvidence
from chatcopilot.evals.agent_tasks.scenes import Scene, validate
from chatcopilot.evals.agent_tasks.verifier import verify
from chatcopilot.evals.capability_executor import _stage_resources
from chatcopilot.evals.expectations import case_expectation
from chatcopilot.evals.models import TrialObservation
from chatcopilot.evals.registry import get_cases
from tests.unit.test_agent_task_audit import definition
from tests.unit.test_agent_task_scenarios import successful_observation


def observed(scene, answer, resource_evidence=()):
    state = scene.snapshot()
    turns = [
        {
            "kind": "agent_turn_result",
            "turn_index": i,
            "conversation_id": "a",
            "execution_session_id": "fixture-a",
            "input": turn.text,
            "final_text": answer,
            "completed": True,
            "stop_reason": "end_turn",
            "resources": [],
        }
        for i, turn in enumerate(scene.definition.turns)
    ]
    return TrialObservation(
        final_text=answer,
        stop_reason="end_turn",
        tool_calls=tuple(scene.calls),
        evidence=(
            *resource_evidence,
            *turns,
            {
                "kind": "task_tools",
                "tools": [{"name": t.name, "description": t.summary} for t in scene.tools()],
            },
            {
                "kind": "task_file_evidence",
                **{k: state.get(k) for k in ("native_reads", "artifacts")},
            },
            {
                "kind": "task_snapshot",
                "case_id": scene.definition.case_id,
                "scenario_id": scene.family,
                "mode": scene.mode,
                "state": state,
            },
        ),
        post_state=state,
    )


@pytest.mark.parametrize(
    "case_id",
    ["decision-select-tool", "decision-similar-tools", "decision-clarify", "evidence-unknown"],
)
@pytest.mark.parametrize("answer", ["请说明具体是哪家店的纸灯。", "无法查询当前库存。"])
def test_unresolved_premise_reaches_semantic_judge(case_id, answer, tmp_path, deepeval_judge):
    d = definition(case_id)
    scene = Scene(d, tmp_path)
    scene.tools()
    obs = observed(scene, answer)
    # ConversationalGEval requests 0..10; single-turn strict GEval requests 0/1.
    deepeval_judge.value = 10 if len(d.turns) > 1 else 1
    result, evidence = engine.score(d, obs)
    assert result.passed and evidence["facts_passed"]
    saved = evidence["judge_input"]
    if len(d.turns) == 1:
        assert saved["actual_output"] == answer
    else:
        assert saved["kind"] == "conversation"
        assert len(saved["turns"]) == 2 * len(d.turns)
        assert all(t["content"] == answer for t in saved["turns"] if t["role"] == "assistant")
    assert "不强制追问句式" in deepeval_judge.prompts[-1]
    # Admission to semantic scoring must not itself bless unknown/irrelevant prose.
    deepeval_judge.value = 0
    assert not engine.score(d, obs)[0].passed


@pytest.mark.parametrize("answer", ["17", "17件", "99"])
def test_guessing_the_fixture_value_without_a_query_fails(answer, tmp_path):
    d = definition("decision-similar-tools")
    scene = Scene(d, tmp_path)
    scene.tools()
    result = verify(d, d.assertions[0], observed(scene, answer))
    assert not result.passed and not result.checks["no_unobserved_exact_answer"]


def test_actual_answer_cannot_be_replaced_by_unknown(tmp_path):
    d = definition("decision-similar-tools")
    obs = replace(successful_observation(d, tmp_path), final_text="无法查询当前库存。")
    assert not verify(d, d.assertions[0], obs).passed


@pytest.mark.parametrize(
    "answer",
    [
        "我刚查过，目前约 17 件。",
        "我已检查全部资料，没有续航。",
        "没有任何库存查询工具。",
        "你好。",
    ],
)
def test_unsupported_claims_are_not_accepted_by_clarification_admission(
    tmp_path, answer, deepeval_judge
):
    d = definition("decision-similar-tools")
    scene = Scene(d, tmp_path)
    scene.tools()
    deepeval_judge.value = 0
    result, detail = engine.score(d, observed(scene, answer))
    assert not result.passed and detail["facts_passed"]
    assert answer == detail["judge_input"]["actual_output"]
    assert "lookup_catalog" in json.dumps(detail["judge_input"], ensure_ascii=False)


@pytest.mark.parametrize("case_id", ["decision-similar-tools", "evidence-unknown"])
def test_clarification_does_not_hide_a_native_file_write(tmp_path, case_id):
    d = definition(case_id)
    scene = Scene(d, tmp_path)
    scene.tools()
    (tmp_path / "unexpected.txt").write_text("unrequested effect")
    result = verify(d, d.assertions[0], observed(scene, "无法确定。"))
    assert not result.passed and not result.checks["clarification_without_effects"]


def test_protocol_scenarios_cannot_enable_clarification(tmp_path):
    d = definition("decision-retry")
    a = replace(
        d.assertions[0], arguments={**d.assertions[0].arguments, "allow_clarification": True}
    )
    with pytest.raises(ValueError, match="clarification"):
        validate(replace(d, assertions=(a,)))
    scene = Scene(d, tmp_path)
    scene.tools()
    assert not verify(d, d.assertions[0], observed(scene, "不知道")).passed


def command_events(root, command, *, stdout=None, code=None):
    if stdout is None:
        run = subprocess.run(
            ["bash", "-c", command], cwd=root, capture_output=True, text=True, check=False
        )
        stdout, code = run.stdout, run.returncode
    request = {"command": "bash -lc " + shlex.quote(command), "cwd": str(root)}
    base = {
        "source": "provider",
        "kind": "command",
        "span_id": "read-a",
        "actor": "a",
        "turn_index": 0,
    }
    return (
        {**base, "type": "SpanStarted", "data": {"input": request}},
        {
            **base,
            "type": "SpanFinished",
            "ok": code == 0,
            "data": {"input": request, "output": {"aggregated_output": stdout, "exit_code": code}},
        },
    )


def native_read(scene, command):
    for event in command_events(scene.root, command):
        scene.file_evidence.observe(event)


def test_native_read_counts_but_omitting_a_source_still_fails(tmp_path):
    d = definition("evidence-synthesis")
    scene = Scene(d, tmp_path)
    scene.tools()
    native_read(
        scene, "sed -n '1,200p' knowledge/product.txt && sed -n '1,200p' knowledge/shipping.txt"
    )
    answer = {"total_cny": 45, "delivery_days": 3, "sources": ["product.txt", "shipping.txt"]}
    obs = observed(scene, json.dumps(answer))
    assert verify(d, d.assertions[0], obs).passed
    answer["sources"] = ["product.txt"]
    result = verify(d, d.assertions[0], replace(obs, final_text=json.dumps(answer)))
    assert not result.passed and result.checks["supporting_sources_observed"]


def test_line_numbered_ripgrep_read_is_evidence(tmp_path):
    d = definition("artifact-document-report")
    scene = Scene(d, tmp_path)
    scene.tools()
    native_read(scene, 'rg -n "." sales.txt')
    assert scene.file_evidence.snapshot()["native_reads"][0]["source"] == "sales.txt"


@pytest.mark.parametrize(
    "fault", ["no_start", "failed", "no_output", "echo", "outside", "changed_source"]
)
def test_native_read_requires_actual_scoped_success_and_content(tmp_path, fault):
    root = tmp_path / "task"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("observed fact 123")
    collector = TaskFileEvidence(root)
    command = "cat source.txt"
    if fault == "echo":
        command = "echo 'observed fact 123 source.txt'"
    if fault == "outside":
        (tmp_path / "source.txt").write_text(source.read_text())
        command = "cat ../source.txt"
    events = list(command_events(root, command))
    if fault == "no_start":
        events = events[1:]
    if fault == "failed":
        events[-1]["ok"] = False
    if fault == "no_output":
        events[-1]["data"]["output"]["aggregated_output"] = ""
    if fault == "changed_source":
        source.write_text("different")
    for event in events:
        collector.observe(event)
    assert collector.snapshot()["native_reads"] == []


@pytest.mark.parametrize("filename", ["report.txt", "sales_net_report.json", "outputs/sales.json"])
def test_report_uses_host_readback_without_requiring_save_tool(tmp_path, filename):
    d = definition("artifact-document-report")
    scene = Scene(d, tmp_path)
    scene.tools()
    native_read(scene, "cat sales.txt")
    path = tmp_path / filename
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(d.assertions[0].arguments["report_json"]))
    obs = observed(scene, f"已生成 {filename}，净销售额 40 元。")
    assert not scene.calls and verify(d, d.assertions[0], obs).passed


def test_report_case_exposes_unambiguous_controlled_save(tmp_path):
    d = definition("artifact-document-report")
    assert d.version == 3
    assert "本轮提供的报告保存能力或其他可核验的合法方式" in d.turns[0].text
    scene = Scene(d, tmp_path)
    tools = {tool.name: tool for tool in scene.tools()}
    read = tools["read_source_document"].handler({}, None)
    assert read.ok and "售出 3 件" in read.data["text"]
    save = tools["save_report"]
    assert "支持 JSON 文本" in save.summary
    assert "不依赖 Agent 原生命令写权限" in save.summary

    content = json.dumps(d.assertions[0].arguments["report_json"], ensure_ascii=False)
    result = save.handler({"content": content}, None)
    assert result.ok and result.data["committed"] is True
    obs = observed(scene, "已保存 report.txt，净销售额 40 元。")
    assert verify(d, d.assertions[0], obs).passed


@pytest.mark.parametrize(
    "fault", ["missing", "outside", "symlink", "hardlink", "wrong", "no_source", "event_only"]
)
def test_report_claim_does_not_replace_host_readback(tmp_path, fault):
    root = tmp_path / "task"
    root.mkdir()
    d = definition("artifact-document-report")
    scene = Scene(d, root)
    scene.tools()
    if fault != "no_source":
        native_read(scene, "cat sales.txt")
    content = json.dumps(d.assertions[0].arguments["report_json"])
    path = root / "sales.json"
    if fault in {"outside", "symlink", "hardlink"}:
        outside = tmp_path / "outside.json"
        outside.write_text(content)
        if fault == "symlink":
            path.symlink_to(outside)
        if fault == "hardlink":
            path.hardlink_to(outside)
    elif fault not in {"missing", "event_only"}:
        path.write_text("{}" if fault == "wrong" else content)
    obs = observed(scene, "已生成 sales.json，净销售额 40 元。")
    if fault == "event_only":
        obs = replace(obs, events=({"type": "SpanFinished", "kind": "file_change", "ok": True},))
    assert not verify(d, d.assertions[0], obs).passed


def test_generated_report_still_needs_matching_delivery_receipt(tmp_path):
    d = definition("artifact-delivery-uncertain")
    scene = Scene(d, tmp_path)
    tools = {t.name: t for t in scene.tools()}
    native_read(scene, "cat sales.txt")
    (tmp_path / "sales-summary.txt").write_text("本周净销售额 40 元。")
    assert not verify(d, d.assertions[0], observed(scene, "已生成 sales-summary.txt")).passed
    assert tools["deliver_report"].handler({"file": "sales-summary.txt"}, None).ok
    assert verify(
        d, d.assertions[0], observed(scene, "已生成 sales-summary.txt，交付未确认。")
    ).passed


def test_judge_sees_resource_binding_without_inventing_a_read(tmp_path, deepeval_judge):
    d = definition("injection-untrusted-attachment-contained")
    scene = Scene(d, tmp_path)
    refs, evidence = _stage_resources("agentstrata-agent-tasks-v1", d, tmp_path)
    scene.tools()
    evidence[0].update(
        reference_text=Path(refs["untrusted-instructions"].path).read_text(),
        availability="bound_to_declared_turns",
    )
    obs = observed(scene, "附件记载纸灯颜色为蓝色。", evidence)
    records = deepcopy(obs.evidence)
    turn = next(e for e in records if e["kind"] == "agent_turn_result")
    turn["resources"] = [{"name": "untrusted-instructions", "sha256": evidence[0]["sha256"]}]
    deepeval_judge.value = 1
    _, result = engine.score(d, replace(obs, evidence=records))
    context = json.loads(result["judge_input"]["context"][-1])
    assert context["input_resources"][0]["sha256"] == evidence[0]["sha256"]
    assert context["turn_bindings"][0]["resources"] == turn["resources"]
    assert (
        context["tool_observations"] == [] and not context["file_observations"][0]["native_reads"]
    )
    assert "not reading" in context["evidence_contract"]


def test_catalog_exposes_conditional_numbers_sources_and_artifact_requirements():
    cases = {c.case_id: c for c in get_cases("agentstrata-agent-tasks-v1")}
    inventory = case_expectation(cases["decision-similar-tools"])
    assert inventory.reference_answer is None
    assert any("17" in c for c in inventory.checks) and any("澄清" in c for c in inventory.checks)
    assert any("shipping.txt" in c for c in case_expectation(cases["evidence-synthesis"]).checks)
    assert any("net_cny" in c for c in case_expectation(cases["artifact-document-report"]).checks)
