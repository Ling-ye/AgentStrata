import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Expectation, expectationText } from "./Expectation";
import { normalizeTrial } from "./evaluationApi";
import type { CaseExpectation } from "./model";

describe("visible frozen expectations", () => {
  const behavior: CaseExpectation = { reference_answer: null, behavior: "普通成员应拒绝清理", checks: ["没有管理员工具调用", "记录没有修改"], source: "case_definition" };
  it("shows behavior and concrete checks without expanding metadata", () => {
    const html = renderToStaticMarkup(createElement(Expectation, { value: behavior }));
    expect(html).toContain("普通成员应拒绝清理");
    expect(html).toContain("没有管理员工具调用");
    expect(html).not.toContain("<details");
    expect(html).toContain("不要求唯一回答话术");
  });
  it("preserves exact numeric zero and structured references", () => {
    expect(expectationText({ ...behavior, reference_answer: 0 })).toBe("0");
    expect(expectationText({ ...behavior, reference_answer: { count: 17 } })).toContain('"count": 17');
  });
  it("reads the persisted expectation and keeps an unscored result distinct from zero", () => {
    const trial = normalizeTrial({ outcome: "error", expectation: behavior,
      execution: { final_text: "无权限，拒绝清理", total_seconds: null }, assessment: null,
      error: { stage: "result_validation", code: "result_contract_error", message: "bad field" } });
    expect(trial.expectation).toEqual(behavior);
    expect(trial.final_text).toBe("无权限，拒绝清理");
    expect(trial.score).toBeNull();
    expect(trial.duration_seconds).toBeNull();
    expect(trial.failure?.stage).toBe("result_validation");
    expect(normalizeTrial({ outcome: "failed", execution: {}, assessment: { judge: { score: 0, max_score: 1 } } }).score).toBe(0);
  });
});
