import { describe, expect, it } from "vitest";
import { normalizeTrial } from "./evaluationApi";
import { instructionChecks, instructionLabel, trialSource } from "./trialModel";
import type { EvaluationRecord, EvaluationTrial } from "./model";

describe("frozen evaluation provenance", () => {
  const record = { result: { config_snapshot: { definition_snapshot: { cases: [
    { case_id: "official", metadata: { case_source: { kind: "ifeval_subset", label: "IFEval 固定子集", key: 1001, revision: "frozen" } } },
  ] } } } } as unknown as EvaluationRecord;
  it("restores source from the selected historical definition only", () => {
    const trial = { case_id: "official", evidence: {} } as EvaluationTrial;
    expect(trialSource(record, trial).revision).toBe("frozen");
    expect(trialSource(record, { ...trial, case_id: "unrecorded" })).toEqual({});
  });
  it("shows each actual instruction result without turning failure into success", () => {
    const trial = { evidence: { judge_evidence: { assertions: [{ checks: { instructions: [
      { id: "punctuation:no_comma", parameters: {}, passed: false },
      { id: "keywords:frequency", parameters: { frequency: 3 }, passed: true },
    ] } }] } } } as unknown as EvaluationTrial;
    expect(instructionChecks(trial).map(c => c.passed)).toEqual([false, true]);
    expect(instructionLabel("punctuation:no_comma")).toBe("不使用逗号");
  });
});


it("reads direct model instruction results without replacing strict failures", () => {
  const row = normalizeTrial({ execution: { metadata: { instructions: [{ id: "keywords:frequency", passed: false, loose_passed: true, parameters: { frequency: 2 } }] } } });
  expect(instructionChecks(row)[0]).toMatchObject({ passed: false, loose_passed: true });
});

import { isModelOutput, modelCalls, modelOutputSummary } from "./trialModel";

describe("model output evidence", () => {
  const model = { benchmark: { subject_type: "model" } } as unknown as EvaluationRecord;
  const call = { id: "saved-call", function: { name: "get_weather", arguments: '{"city":"Paris"}' } };
  it("renders the saved historical function without claiming execution", () => {
    const row = normalizeTrial({ execution: { final_text: "", metadata: { tool_calls: [call] } } });
    expect(modelOutputSummary(model, row)).toContain('get_weather({"city":"Paris"})');
    expect(modelCalls(row)[0]).toMatchObject({ id: "saved-call", name: "get_weather", parsed: { city: "Paris" }, raw: call.function.arguments, error: "" });
    expect(row.final_text).toBe("");
    expect(modelOutputSummary(model, row)).not.toContain("已执行");
  });
  it("retains bounded list projections, text, and calls", () => {
    const row = normalizeTrial({ model_output_preview: { kind: "mixed", text: "checking", calls: [{ name: "get_weather", arguments: "{}" }] } });
    expect(modelOutputSummary(model, row)).toBe("checking\nget_weather({})");
  });
  it("distinguishes a recorded empty response and missing evidence", () => {
    expect(modelOutputSummary(model, normalizeTrial({ execution: { metadata: { model_response: { content: "", tool_calls: [] } } } }))).toContain("模型返回空内容");
    expect(modelOutputSummary(model, normalizeTrial({}))).toBe("未记录模型输出");
  });
  it("preserves invalid parameter strings and reports parse errors", () => {
    const row = normalizeTrial({ execution: { metadata: { tool_calls: [{ function: { name: "broken", arguments: '{"x":' } }] } } });
    expect(modelCalls(row)[0]).toMatchObject({ raw: '{"x":', error: "参数 JSON 解析失败，以下保留原始字符串" });
  });
  it("keeps model data visible after a scoring error, with text and calls", () => {
    const row = normalizeTrial({ outcome: "error", error: { stage: "scoring", code: "judge_error", message: "judge failed" }, execution: { metadata: { model_response: { content: "proposed call", tool_calls: [call], finish_reason: "tool_calls" } } } });
    expect(modelOutputSummary(model, row)).toContain("proposed call\nget_weather");
    expect(isModelOutput({} as unknown as EvaluationRecord, row)).toBe(true);
  });
  it("does not interpret Agent tool execution as model-only calls", () => {
    const row = normalizeTrial({ execution: { final_text: "actual result", metadata: { tool_calls: [call] } } });
    expect(isModelOutput({ benchmark: { subject_type: "agent" } } as unknown as EvaluationRecord, row)).toBe(false);
  });
});
