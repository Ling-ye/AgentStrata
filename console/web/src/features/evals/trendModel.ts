import type { EvaluationRecord } from "./model";
import type { TargetSummary } from "./insightsModel";
import { recordTime, durationSummary, durationLabel } from "./insightsModel";

export type SplitDimension = "agent" | "model" | "scale";
export type TrendMetric = "pass_rate" | "quality" | "duration";
export interface TrendPoint extends TargetSummary { key: string; record: EvaluationRecord; timestamp: number }
export interface TrendLine { key: string; label: string; points: TrendPoint[] }
export const pointModel = (point: TargetSummary) => [point.model, point.reasoning_effort].filter(Boolean).join(" / ") || (point.duration?.kind === "runtime" ? "不涉及模型" : "模型未记录");
export const pointScale = (point: TargetSummary) => `${point.case_ids.length} 项 × ${point.repetitions} 次`;

export function buildTrendPoints(records: EvaluationRecord[], selectedCases: string[] = []): TrendPoint[] {
  return records.flatMap(record => {
    if (!record.insights.trend_eligible || !Number.isFinite(recordTime(record))) return [];
    return record.insights.targets.flatMap(target => {
      let point = { ...target };
      if (selectedCases.length) {
        const cases = point.cases.filter(c => selectedCases.includes(c.case_id));
        if (!cases.length) return [];
        const counts = { passed: 0, failed: 0, error: 0, skipped: 0 };
        for (const c of cases) for (const key of Object.keys(counts) as Array<keyof typeof counts>) counts[key] += c.counts[key];
        const observed = Object.values(counts).reduce((a, b) => a + b, 0);
        const scored = cases.reduce((sum, c) => sum + c.quality.scored, 0);
        const weighted = cases.reduce((sum, c) => sum + (c.quality.score ?? 0) * c.quality.scored, 0);
        const durations = cases.map(c => durationSummary(c.duration, c.agent_duration_seconds, Object.values(c.counts).reduce((a, b) => a + b, 0)));
        const known = durations.flatMap(d => d.recorded_seconds === null ? [] : [d.recorded_seconds]);
        const subtotal = known.length ? known.reduce((a, b) => a + b, 0) : null;
        const complete = durations.every(d => d.complete);
        const duration = { kind: durations[0].kind, total_seconds: complete ? subtotal : null,
          recorded_seconds: subtotal, recorded: durations.reduce((sum, d) => sum + d.recorded, 0),
          partial: durations.reduce((sum, d) => sum + d.partial, 0), expected: observed, complete };
        point = { ...point, counts, observed, duration, pass_rate: observed ? counts.passed / observed : null,
          quality: { score: scored ? weighted / scored : null, scored, expected: cases.reduce((sum, c) => sum + c.quality.expected, 0) },
          agent_duration_seconds: cases.every(c => c.agent_duration_seconds !== null)
            ? cases.reduce((sum, c) => sum + (c.agent_duration_seconds ?? 0), 0) : null };
      }
      return [{ ...point, key: `${record.evaluation_id}:${target.target_id}`, record, timestamp: recordTime(record) }];
    });
  }).sort((a, b) => a.timestamp - b.timestamp || a.key.localeCompare(b.key));
}

export function groupTrendPoints(points: TrendPoint[], dimensions: SplitDimension[], metric?: TrendMetric): TrendLine[] {
  const lines = new Map<string, TrendLine>();
  for (const point of points) {
    const values = dimensions.map(d => d === "agent" ? `${point.record.bot_id} / ${point.backend}` : d === "model" ? pointModel(point) : pointScale(point));
    const contract = metric ? point.record.insights.comparison_keys?.[metric] : undefined;
    if (metric && !contract) continue;
    const key = JSON.stringify([...(contract ? [contract] : []), ...values]);
    const line = lines.get(key) ?? { key, label: (values.join(" · ") || "所选评测") + (contract ? ` · 条件 ${contract.slice(0, 6)}` : ""), points: [] };
    line.points.push(point); lines.set(key, line);
  }
  return [...lines.values()];
}

export function pointValue(point: TrendPoint, metric: TrendMetric): number | null {
  return metric === "pass_rate" ? point.pass_rate : metric === "quality" ? (point.quality.scored === point.quality.expected ? point.quality.score : null) : pointDuration(point).recorded_seconds;
}

export function linePaths(points: TrendPoint[], metric: TrendMetric, x: (p: TrendPoint) => number, y: (v: number) => number): string[] {
  const paths: string[] = []; let path = "";
  for (const point of points) {
    const value = pointValue(point, metric);
    if (value === null || (metric === "duration" && !pointDuration(point).complete)) { if (path) paths.push(path); path = ""; continue; }
    path += `${path ? " L" : "M"}${x(point)},${y(value)}`;
  }
  if (path) paths.push(path);
  return paths;
}

export function pointDuration(point: TargetSummary) {
  return durationSummary(point.duration, point.agent_duration_seconds, point.observed);
}
export function durationPointLabel(point: TargetSummary): string {
  const duration = pointDuration(point);
  if (duration.recorded_seconds === null) return "未记录";
  return `${duration.complete ? "" : "至少 "}${durationLabel(duration.recorded_seconds)}`;
}
export function durationCoverage(point: TargetSummary): string {
  const duration = pointDuration(point);
  return `完整记录 ${duration.recorded}/${duration.expected} 项${duration.partial ? `，${duration.partial} 项仅有部分耗时` : ""}`;
}
