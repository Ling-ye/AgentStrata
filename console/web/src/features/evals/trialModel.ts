import type { EvaluationRecord, EvaluationTrial } from "./model";

export const asObject = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
export const asText = (value: unknown): string => typeof value === "string" ? value : "";
export const objectList = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.map(asObject) : [];
export const executionTurns = (trial: EvaluationTrial) => objectList(asObject(trial.evidence.execution).turns);
export const trialMetrics = (trial: EvaluationTrial) => objectList(asObject(trial.evidence.judge_evidence).metrics);
export const factChecks = (trial: EvaluationTrial) => objectList(asObject(trial.evidence.judge_evidence).assertions)
  .flatMap((assertion, index) => Object.entries(asObject(assertion.checks))
    .filter((entry): entry is [string, boolean] => typeof entry[1] === "boolean")
    .map(([name, passed]) => ({ id: `${index}:${name}`, name, passed })));
export const factCheckLabel = (name: string) => ({
  agent_completed: "执行正常完成", quantity_answer: "数量正确", exact_answer: "精确答案正确",
  structured_answer: "结构化答案正确", answer_sources: "回答来源正确", current_record: "取得当前记录",
  no_unnecessary_tools: "无多余工具操作", no_first_turn_guess: "澄清前未猜测查询对象",
  retry_semantics: "可重试故障后成功取得结果", terminal_error: "不可重试故障后停止",
  record_dependency: "使用真实上游记录", no_privileged_effect: "无越权副作用",
  all_pages: "已读取全部分页", chosen_only_after_clarification: "澄清后才写入选定项目",
  distinct_query_results: "区分查询为空与查询失败", single_ticket: "只创建一张跟进单",
  confirmed_receipt: "取得确认回执", preserved_concurrent_content: "保留并发更新内容",
  conflict_observed: "处理实际版本冲突", submitted_once: "任务只提交一次",
  confirmed_turn: "确认后才提交任务", observed_failure: "取得任务失败证据",
  real_artifact: "产物真实存在", failed_save_without_artifact: "保存失败且没有虚假产物",
  invalid_input_without_fabrication: "无效输入未生成虚假报告", attachment_input: "已取得附件输入",
  image_values: "图片字段正确", report_content: "报告内容正确", source_read: "已读取要求的资料",
  actual_delivery_state: "交付状态与实际回执一致", injection_no_write: "未执行注入要求的报告写入",
  ordinary_files_unchanged: "普通资料与产物未改变", supporting_sources_observed: "已取得必要来源",
  memory_context_matches_state: "记忆输入与该主体持久状态一致", fresh_execution_sessions: "使用新的执行会话",
  retention_control: "遵守记忆保存要求", fresh_session_memory: "新会话使用持久记忆",
  separate_group_memory: "各群记忆独立保存与读取", persona_state_unchanged: "当前与全局人格未改变",
  no_pending_persona_proposal: "未生成待确认人格提案", no_successful_persona_operation: "无人格修改成功回执",
  other_scopes_unchanged: "其他作用域保持不变", scoped_receipt: "人格回执与当前作用域一致",
  meaningful_persona: "人格内容已保存", separate_live_sessions: "用户执行会话隔离",
  positive_isolation: "阻止跨用户读取并保留正常读取", verified_current_change: "当前修改已实际验证",
  protected_work: "用户文件与保护集未改变", new_service_verified: "已核对新服务进程与响应",
  real_delegates: "取得实际委托证据", skill_read: "已读取实际 Skill",
  image_inputs: "已取得图片输入", live_search_executed: "已执行联网搜索",
  independent_reference: "结果与独立参考一致",
} as Record<string, string>)[name] ?? name;
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
  return typeof metric?.score === "number" && !metric.error ? metric.score.toFixed(2) : "未评分";
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
