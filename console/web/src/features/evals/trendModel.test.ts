import { describe, expect, it } from "vitest";
import { normalizeEvaluation } from "./model";
import { buildTrendPoints, groupTrendPoints, linePaths, pointValue, pointDuration, durationPointLabel, durationCoverage } from "./trendModel";
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
    const t = normalizeTrial({ case_id: "a", execution: { metadata: { execution: { turns: [{ input: "effective request" }] } } } });
    expect(recordedInput(r, t)).toEqual({ text: "effective request", source: "实际输入" });
    t.evidence = {};
    expect(recordedInput(r, t).source).toContain("实际发送未记录");
    r.result = null;
    expect(recordedInput(r, t).text).toBe("");
  });
});

describe("comparable benchmark trends", () => {
  it("never connects distinct subjects or suites even in exploration", () => {
    const model = record("1"), agent = record("2"), otherSuite = record("3"), legacy = record("4");
    model.benchmark = { suite_id: "shared", subject_type: "model" };
    agent.benchmark = { suite_id: "shared", subject_type: "agent" };
    otherSuite.benchmark = { suite_id: "another", subject_type: "agent" };
    legacy.benchmark = { suite_id: "shared" };
    for (const item of [model, agent, otherSuite, legacy]) item.request.suite_id = item.benchmark?.suite_id;
    expect(groupTrendPoints(buildTrendPoints([model, agent, otherSuite, legacy]), [])).toHaveLength(4);
  });
  it("keeps different case and scorer contracts separate while retaining exploration", () => {
    const first = record("1"), second = record("2"), legacy = record("3");
    first.insights.comparison_keys = { pass_rate: "same-native", quality: "judge-a" };
    second.insights.comparison_keys = { pass_rate: "same-native", quality: "judge-b" };
    const points = buildTrendPoints([first, second, legacy]);
    expect(groupTrendPoints(points, [], "pass_rate")).toHaveLength(1);
    expect(groupTrendPoints(points, [], "pass_rate")[0].points).toHaveLength(2);
    expect(groupTrendPoints(points, [], "quality")).toHaveLength(2);
    expect(groupTrendPoints(points, [])[0].points).toHaveLength(3);
    second.insights.comparison_keys.pass_rate = "other-case-set";
    expect(groupTrendPoints(buildTrendPoints([first, second]), [], "pass_rate")).toHaveLength(2);
  });
  it("does not present incomplete quality coverage as a complete quality point", () => {
    const point = buildTrendPoints([record("1")])[0];
    expect(pointValue(point, "quality")).toBeNull();
    point.quality.scored = point.quality.expected;
    expect(pointValue(point, "quality")).toBe(.9);
  });
});

describe("cumulative execution duration", () => {
  it("plots partial measurements as a lower bound and breaks duration lines", () => {
    const records = [record("1"), record("2"), record("3")];
    records.forEach((r, i) => {
      r.insights.targets[0].duration = { kind: "agent", total_seconds: i === 1 ? null : i + 2,
        recorded_seconds: i + 2, recorded: i === 1 ? 1 : 2, expected: 2, partial: 0, complete: i !== 1 };
    });
    const points = buildTrendPoints(records);
    expect(pointValue(points[1], "duration")).toBe(3);
    expect(durationPointLabel(points[1])).toContain("至少");
    expect(durationCoverage(points[1])).toBe("完整记录 1/2 项");
    expect(linePaths(points, "duration", p => Number(p.record.evaluation_id), v => v)).toEqual(["M1,2", "M3,4"]);
  });
  it("keeps zero, missing duration and filtered Case coverage separate", () => {
    const r = record("1");
    r.insights.targets[0].cases.push({ case_id: "case-1", counts: { passed: 0, failed: 0, error: 1, skipped: 0 },
      quality: { score: null, scored: 0, expected: 0 }, agent_duration_seconds: null });
    const all = buildTrendPoints([r], ["case-0", "case-1"])[0];
    expect(pointValue(all, "duration")).toBe(2);
    expect(pointDuration(all).complete).toBe(false);
    expect(pointDuration(all).recorded).toBe(1);
    const selected = buildTrendPoints([r], ["case-0"])[0];
    expect(pointDuration(selected).complete).toBe(true);
    expect(pointDuration(selected).expected).toBe(1);
    selected.duration = { kind: "agent", total_seconds: 0, recorded_seconds: 0, recorded: 1, expected: 1, partial: 0, complete: true };
    expect(pointValue(selected, "duration")).toBe(0);
    const unrecorded = buildTrendPoints([record("1")])[0];
    expect(pointValue(unrecorded, "duration")).toBeNull();
    expect(durationPointLabel(unrecorded)).toBe("未记录");
  });
});
