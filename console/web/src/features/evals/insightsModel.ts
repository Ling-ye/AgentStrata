import type { EvaluationRecord } from "./model";

export type OutcomeCounts = Record<"passed" | "failed" | "error" | "skipped", number>;
export interface EvaluationInsights {
  counts: OutcomeCounts | null;
  observed: number | null;
  planned: number | null;
  pass_rate: number | null;
  complete: boolean;
  source: string;
  verdict: string;
  capabilities: Record<string, OutcomeCounts>;
  series_key: string | null;
  trend_eligible: boolean;
  exclusion_reason: string;
  configuration_fingerprint: string | null;
  benchmark_fingerprint: string | null;
}
export interface SourceRevision {
  status: string;
  commit: string | null;
  dirty: boolean | null;
  captured_at: string | null;
}
const object = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const number = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const text = (value: unknown): string | null => typeof value === "string" && value ? value : null;
function counts(value: unknown): OutcomeCounts | null {
  const item = object(value);
  const keys = ["passed", "failed", "error", "skipped"] as const;
  if (!keys.every(key => number(item[key]) !== null && Number.isInteger(item[key]))) return null;
  return Object.fromEntries(keys.map(key => [key, item[key]])) as OutcomeCounts;
}
export function normalizeInsights(value: unknown): EvaluationInsights {
  const item = object(value);
  return {
    counts: counts(item.counts), observed: number(item.observed), planned: number(item.planned),
    pass_rate: number(item.pass_rate) !== null && Number(item.pass_rate) <= 1 ? Number(item.pass_rate) : null,
    complete: item.complete === true, source: text(item.source) ?? "not_recorded", verdict: text(item.verdict) ?? "",
    capabilities: Object.fromEntries(Object.entries(object(item.capabilities)).flatMap(([key, value]) => {
      const group = counts(value); return group ? [[key, group]] : [];
    })),
    series_key: text(item.series_key), trend_eligible: item.trend_eligible === true,
    exclusion_reason: text(item.exclusion_reason) ?? "missing_definition",
    configuration_fingerprint: text(item.configuration_fingerprint), benchmark_fingerprint: text(item.benchmark_fingerprint),
  };
}
export function normalizeSourceRevision(value: unknown): SourceRevision {
  const item = object(value);
  return { status: text(item.status) ?? "not_recorded", commit: text(item.commit),
    dirty: typeof item.dirty === "boolean" ? item.dirty : null, captured_at: text(item.captured_at) };
}
export const OUTCOME_LABELS: Record<string, string> = { passed: "通过", failed: "失败", error: "异常", skipped: "跳过" };
export const VERDICT_LABELS: Record<string, string> = {
  passed: "全部门禁通过", failed: "存在未通过项", "error/indeterminate": "结果不确定", cancelled: "已取消", in_progress: "评测中",
};
export const EXCLUSION_LABELS: Record<string, string> = {
  not_completed: "未完整结束", dry_run: "试运行", incomplete_trials: "测试记录不完整",
  invalid_trials: "测试记录标识或结果异常", missing_definition: "缺少测试定义快照", missing_target: "缺少模型信息", unsupported_kind: "历史对比评测",
};
export function evaluationSuiteId(record: EvaluationRecord): string {
  return text(record.selection.id) ?? text(record.request.suite_id) ?? text(record.result?.suite) ?? "";
}
export function modelLabel(record: EvaluationRecord): string {
  return record.targets.map(target => [target.backend, target.model, target.reasoning_effort].filter(Boolean).join(" / ")).join(" · ") || "模型未记录";
}
export function revisionLabel(record: EvaluationRecord): string {
  const source = record.source_revision;
  const revision = source.commit?.slice(0, 10) ?? "版本未记录";
  return revision + (source.dirty === true ? " · 有未提交改动" : source.commit && source.dirty === null ? " · 工作区状态未知" : "");
}
export function rateLabel(rate: number | null): string {
  return rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
}
export function recordTime(record: EvaluationRecord): number {
  return Date.parse(record.started_at || record.created_at);
}
export function dateLabel(value: string | null | undefined): string {
  if (!value) return "未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间无效" : date.toLocaleString("zh-CN");
}
export function durationLabel(value: number | null): string {
  return value === null ? "—" : value < 60 ? `${value.toFixed(1)} 秒` : `${(value / 60).toFixed(1)} 分`;
}
export interface TrendSeries { key: string; records: EvaluationRecord[]; label: string }
export function trendSeries(records: EvaluationRecord[]): TrendSeries[] {
  const groups = new Map<string, EvaluationRecord[]>();
  for (const record of records) {
    const key = record.insights.series_key;
    if (!key || !record.insights.trend_eligible || !Number.isFinite(recordTime(record))) continue;
    const items = groups.get(key) ?? []; items.push(record); groups.set(key, items);
  }
  return [...groups].map(([key, items]) => {
    items.sort((a, b) => recordTime(a) - recordTime(b) || a.evaluation_id.localeCompare(b.evaluation_id));
    const last = items[items.length - 1];
    return { key, records: items, label: `${modelLabel(last)} · ${last.insights.planned ?? "—"} 次 · ${key.slice(0, 8)} · ${items.length} 条` };
  }).sort((a, b) => recordTime(b.records[b.records.length - 1]) - recordTime(a.records[a.records.length - 1]));
}
export function versionChanges(previous: EvaluationRecord | undefined, current: EvaluationRecord): string[] {
  if (!previous) return [];
  const changes: string[] = [];
  const before = previous.source_revision, after = current.source_revision;
  if (before.commit && after.commit && before.commit !== after.commit) changes.push("代码版本变化");
  const oldConfig = previous.insights.configuration_fingerprint, newConfig = current.insights.configuration_fingerprint;
  if (oldConfig && newConfig && oldConfig !== newConfig) changes.push("配置或运行实现变化");
  if (after.dirty) changes.push("含未提交改动");
  return changes;
}
