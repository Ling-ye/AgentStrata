import type { EvaluationRecord, EvaluationTrial } from "./model";

export const asObject = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
export const asText = (value: unknown): string => typeof value === "string" ? value : "";
export const objectList = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.map(asObject) : [];
export const executionTurns = (trial: EvaluationTrial) => objectList(asObject(trial.evidence.execution).turns);
export const trialMetrics = (trial: EvaluationTrial) => objectList(asObject(trial.evidence.judge_evidence).metrics);
export const captureLabel = (state: string) => ({ not_recorded: "未采集", truncated: "已截断", failed: "采集失败", expired: "已到期" }[state] ?? "");

export const isModelOutput = (record: EvaluationRecord, trial: EvaluationTrial) =>
  record.benchmark?.subject_type === "model" || !!trial.evidence.model_response;

export function modelCalls(trial: EvaluationTrial) {
  const response = asObject(trial.evidence.model_response);
  return objectList(response.tool_calls ?? trial.evidence.tool_calls).map(call => {
    const fn = call.function ? asObject(call.function) : call;
    const value = fn.arguments ?? fn.args ?? {};
    const raw = typeof value === "string" ? value : JSON.stringify(value);
    let parsed: unknown = value, error = "";
    try {
      parsed = typeof value === "string" ? JSON.parse(value) : value;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) error = "参数不是 JSON 对象";
    } catch { error = "参数 JSON 解析失败，以下保留原始字符串"; }
    return { id: asText(call.id), name: asText(fn.name), raw, parsed, error };
  });
}

export function modelOutputSummary(record: EvaluationRecord, trial: EvaluationTrial): string {
  if (!isModelOutput(record, trial)) return trial.final_text || "未记录";
  const preview = asObject(trial.model_output_preview);
  const response = asObject(trial.evidence.model_response);
  const text = typeof response.content === "string" ? response.content : trial.final_text;
  const calls = preview.kind ? objectList(preview.calls) : modelCalls(trial).map(c => ({ name: c.name, arguments: c.raw }));
  const parts = [asText(preview.text) || text, ...calls.map(c => `${asText(c.name) || "未命名调用"}(${asText(c.arguments)})`)].filter(Boolean);
  if (parts.length) return parts.join("\n");
  const execution = asObject(trial.evidence.execution);
  const recorded = !!trial.evidence.model_response || (execution.state === "recorded" && "tool_calls" in trial.evidence
    && executionTurns(trial).some(t => t.completed === true));
  return preview.kind === "empty" || recorded ? "模型返回空内容（无函数调用）" : "未记录模型输出";
}

export function recordedInput(record: EvaluationRecord, trial: EvaluationTrial): { text: string; source: string } {
  const turns = executionTurns(trial);
  if (turns.length) return { text: asText(turns[0].input), source: "实际输入" };
  if (asText(trial.evidence.input)) return { text: asText(trial.evidence.input), source: "实际输入" };
  if (trial.input_preview) return { text: trial.input_preview, source: "实际输入" };
  const snapshot = asObject(asObject(record.result?.config_snapshot).definition_snapshot);
  const definition = objectList(snapshot.cases).find(c => c.case_id === trial.case_id);
  const input = definition ? asText(objectList(definition.turns)[0]?.text) || asText(definition.input) : "";
  return { text: input, source: input ? "当时用例定义；实际发送未记录" : "输入未记录" };
}

export function qualityLabel(trial: EvaluationTrial): string {
  const metric = trialMetrics(trial).find(m => m.kind === "quality");
  return typeof metric?.score === "number" && !metric.error ? metric.score.toFixed(2) : "—";
}

export function trialSource(record: EvaluationRecord, trial: EvaluationTrial): Record<string, unknown> {
  const direct = asObject(trial.evidence.case_source);
  if (direct.kind) return direct;
  const snapshot = asObject(asObject(record.result?.config_snapshot).definition_snapshot);
  const definition = objectList(snapshot.cases).find(c => c.case_id === trial.case_id);
  return asObject(asObject(definition?.metadata).case_source);
}

export const instructionChecks = (trial: EvaluationTrial) => {
  const direct = objectList(trial.evidence.instructions);
  return direct.length ? direct : objectList(asObject(trial.evidence.judge_evidence).assertions)
    .flatMap(assertion => objectList(asObject(assertion.checks).instructions));
};

export const instructionLabel = (id: string): string => ({
  "punctuation:no_comma": "不使用逗号", "change_case:english_lowercase": "英文小写",
  "change_case:english_capital": "英文大写", "detectable_format:json_format": "JSON 格式",
  "detectable_format:number_bullet_lists": "列表项数量", "startend:end_checker": "指定结尾",
  "keywords:frequency": "关键词频次", "keywords:existence": "必需关键词",
}[id] ?? id);
