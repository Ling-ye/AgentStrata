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
  const row = normalizeTrial({ evidence: { instructions: [{ id: "keywords:frequency", passed: false, loose_passed: true, parameters: { frequency: 2 } }] } });
  expect(instructionChecks(row)[0]).toMatchObject({ passed: false, loose_passed: true });
});
