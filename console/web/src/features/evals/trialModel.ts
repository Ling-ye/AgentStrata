import type { EvaluationRecord, EvaluationTrial } from "./model";

export const asObject = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
export const asText = (value: unknown): string => typeof value === "string" ? value : "";
export const objectList = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.map(asObject) : [];
export const executionTurns = (trial: EvaluationTrial) => objectList(asObject(trial.evidence.execution).turns);
export const trialMetrics = (trial: EvaluationTrial) => objectList(asObject(trial.evidence.judge_evidence).metrics);
export const captureLabel = (state: string) => ({ not_recorded: "未采集", truncated: "已截断", failed: "采集失败", expired: "已到期" }[state] ?? "");

export function recordedInput(record: EvaluationRecord, trial: EvaluationTrial): { text: string; source: string } {
  const turns = executionTurns(trial);
  if (turns.length) return { text: asText(turns[0].input), source: "实际输入" };
  if (trial.input_preview) return { text: trial.input_preview, source: "实际输入" };
  const snapshot = asObject(asObject(record.result?.config_snapshot).definition_snapshot);
  const definition = objectList(snapshot.cases).find(c => c.case_id === trial.case_id);
  const input = definition ? asText(objectList(definition.turns)[0]?.text) || asText(definition.input) : "";
  return { text: input, source: input ? "当时用例定义；实际发送未记录" : "输入未记录" };
}

export function qualityLabel(trial: EvaluationTrial): string {
  const metric = trialMetrics(trial).find(m => m.kind === "quality");
  return typeof metric?.score === "number" && !metric.error ? `${(metric.score * 100).toFixed(0)}%` : "—";
}
