import { describe, expect, it } from "vitest";
import { normalizeEvaluation } from "./model";
import { evaluationSuiteId, normalizeInsights, rateLabel, revisionLabel, versionChanges } from "./insightsModel";

const record = (id: string, time: string, overrides: Record<string, unknown> = {}) => normalizeEvaluation({
  evaluation_id: id, created_at: time, kind: "suite", status: "completed",
  selection: { kind: "suite", id: "agentstrata-capabilities-v1" },
  insights: { series_key: "fixture-series", trend_eligible: true, counts: { passed: 1, failed: 1, error: 0, skipped: 0 },
    observed: 2, planned: 2, pass_rate: 0.5, complete: true, configuration_fingerprint: "config-a" },
  source_revision: { commit: "a".repeat(40), dirty: false, status: "recorded" },
  ...overrides,
});

describe("evaluation progress presentation", () => {
  it("retains explicit zero counts and does not turn missing data into success", () => {
    expect(normalizeInsights({}).counts).toBeNull();
    expect(normalizeInsights({}).pass_rate).toBeNull();
    expect(normalizeInsights({ pass_rate: 2 }).pass_rate).toBeNull();
    expect(normalizeInsights({ counts: { passed: 0, failed: 2, error: 0, skipped: 0 }, pass_rate: 0 }).pass_rate).toBe(0);
    expect(rateLabel(null)).toBe("—");
    expect(rateLabel(0)).toBe("0.0%");
  });
  it("uses the list selection to identify the track without full result bodies", () => {
    const item = record("a", "2026-09-01T00:00:00Z");
    expect(evaluationSuiteId(item)).toBe("agentstrata-capabilities-v1");
    expect(item.result).toBeNull();
  });
  it("labels unknown historical versions and annotates code/configuration changes", () => {
    const before = record("before", "2026-09-01T00:00:00Z");
    const after = record("after", "2026-09-02T00:00:00Z", {
      source_revision: { commit: "b".repeat(40), dirty: true },
      insights: { configuration_fingerprint: "config-b" },
    });
    expect(versionChanges(before, after)).toEqual(["代码版本变化", "配置或运行实现变化", "含未提交改动"]);
    expect(revisionLabel(after)).toContain("有未提交改动");
    expect(revisionLabel(record("legacy", "2026-09-01T00:00:00Z", { source_revision: undefined }))).toBe("版本未记录");
  });
});
