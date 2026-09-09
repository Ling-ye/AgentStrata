import type { EvaluationRecord } from "./model";
import type { TargetSummary } from "./insightsModel";
import { recordTime } from "./insightsModel";

export type SplitDimension = "agent" | "model" | "scale";
export type TrendMetric = "pass_rate" | "quality" | "duration";
export interface TrendPoint extends TargetSummary { key: string; record: EvaluationRecord; timestamp: number }
export interface TrendLine { key: string; label: string; points: TrendPoint[] }
export const pointModel = (point: TargetSummary) => [point.model, point.reasoning_effort].filter(Boolean).join(" / ") || "模型未记录";
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
        point = { ...point, counts, observed, pass_rate: observed ? counts.passed / observed : null,
          quality: { score: scored ? weighted / scored : null, scored, expected: cases.reduce((sum, c) => sum + c.quality.expected, 0) },
          agent_duration_seconds: cases.every(c => c.agent_duration_seconds !== null)
            ? cases.reduce((sum, c) => sum + (c.agent_duration_seconds ?? 0), 0) : null };
      }
      return [{ ...point, key: `${record.evaluation_id}:${target.target_id}`, record, timestamp: recordTime(record) }];
    });
  }).sort((a, b) => a.timestamp - b.timestamp || a.key.localeCompare(b.key));
}

export function groupTrendPoints(points: TrendPoint[], dimensions: SplitDimension[]): TrendLine[] {
  const lines = new Map<string, TrendLine>();
  for (const point of points) {
    const values = dimensions.map(d => d === "agent" ? `${point.record.bot_id} / ${point.backend}` : d === "model" ? pointModel(point) : pointScale(point));
    const key = JSON.stringify(values);
    const line = lines.get(key) ?? { key, label: values.join(" · ") || "所选评测", points: [] };
    line.points.push(point); lines.set(key, line);
  }
  return [...lines.values()];
}

export function pointValue(point: TrendPoint, metric: TrendMetric): number | null {
  return metric === "pass_rate" ? point.pass_rate : metric === "quality" ? point.quality.score : point.agent_duration_seconds;
}

export function linePaths(points: TrendPoint[], metric: TrendMetric, x: (p: TrendPoint) => number, y: (v: number) => number): string[] {
  const paths: string[] = []; let path = "";
  for (const point of points) {
    const value = pointValue(point, metric);
    if (value === null) { if (path) paths.push(path); path = ""; continue; }
    path += `${path ? " L" : "M"}${x(point)},${y(value)}`;
  }
  if (path) paths.push(path);
  return paths;
}
