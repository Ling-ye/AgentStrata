import { describe, expect, it } from "vitest";
import { normalizeEvaluation } from "./model";
import { buildTrendPoints, groupTrendPoints, linePaths } from "./trendModel";
import { normalizeTrial } from "./evaluationApi";
import { recordedInput } from "./trialModel";

function record(id: string, model = "model-a", bot = "bot-a", count = 2) {
  return normalizeEvaluation({ evaluation_id: id, bot_id: bot, kind: "suite", status: "completed", created_at: `2026-09-0${id}T00:00:00Z`,
    source_revision: { commit: id.repeat(40) }, insights: { series_key: `old-group-${id}`, trend_eligible: true,
      targets: [{ target_id: "main", backend: "native", model, observed: count, repetitions: 1,
        counts: { passed: 1, failed: count - 1, error: 0, skipped: 0 }, pass_rate: 1 / count,
        quality: { score: .9, scored: 1, expected: 2 }, case_ids: Array.from({ length: count }, (_, i) => `case-${i}`),
        cases: [{ case_id: "case-0", counts: { passed: 1, failed: 0, error: 0, skipped: 0 }, quality: { score: .9, scored: 1, expected: 1 }, agent_duration_seconds: 2 }],
      }] } });
}

describe("free composition of evaluation trends", () => {
  it("connects different Git and benchmark fingerprints, with explicit dimensions only", () => {
    const points = buildTrendPoints([record("2"), record("1"), record("3", "model-b"), record("4", "model-a", "bot-b", 3)]);
    expect(points.map(p => p.record.evaluation_id)).toEqual(["1", "2", "3", "4"]);
    expect(groupTrendPoints(points, [])).toHaveLength(1);
    expect(groupTrendPoints(points, ["model"])).toHaveLength(2);
    expect(groupTrendPoints(points, ["agent", "model"])).toHaveLength(3);
    expect(groupTrendPoints(points, ["scale"])).toHaveLength(2);
  });
  it("has one point per target and excludes incomplete runs", () => {
    const r = record("1"); r.insights.targets.push({ ...r.insights.targets[0], target_id: "secondary" });
    expect(buildTrendPoints([r]).map(p => p.key)).toEqual(["1:main", "1:secondary"]);
    r.insights.trend_eligible = false;
    expect(buildTrendPoints([r])).toEqual([]);
  });
  it("filters Case aggregates without fetching any bodies and preserves missing quality gaps", () => {
    const points = buildTrendPoints([record("1"), record("2"), record("3")], ["case-0"]);
    expect(points[0].pass_rate).toBe(1);
    expect(points[0].quality.expected).toBe(1);
    points[1].quality.score = null;
    expect(linePaths(points, "quality", p => Number(p.record.evaluation_id), v => v)).toEqual(["M1,0.9", "M3,0.9"]);
    expect(buildTrendPoints([record("1")], ["absent"])).toEqual([]);
  });
  it("retains more than 200 selected points", () => {
    const r = record("1");
    const values = Array.from({ length: 230 }, (_, i) => ({ ...r, evaluation_id: String(i) }));
    expect(groupTrendPoints(buildTrendPoints(values), [])[0].points).toHaveLength(230);
  });
});

describe("recorded Case input", () => {
  it("uses execution input before a frozen template and never looks up current definitions", () => {
    const r = record("1"); r.result = { config_snapshot: { definition_snapshot: { cases: [{ case_id: "a", input: "frozen template" }] } } };
    const t = normalizeTrial({ case_id: "a", evidence: { execution: { turns: [{ input: "effective request" }] } } });
    expect(recordedInput(r, t)).toEqual({ text: "effective request", source: "实际输入" });
    t.evidence = {};
    expect(recordedInput(r, t).source).toContain("实际发送未记录");
    r.result = null;
    expect(recordedInput(r, t).text).toBe("");
  });
});
