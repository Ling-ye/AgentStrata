import type { EvaluationRecord, EvaluationSubject, EvaluationSuite } from "./model";
import { asObject } from "./trialModel";

export const SUBJECTS = [
  { id: "model", title: "LLM测评", color: "orange", description: "直接评估大语言模型的回答、指令遵循与函数调用。" },
  { id: "agent", title: "Agent测评", color: "arcoblue", description: "评估 Agent 在给定工具与上下文环境中的任务完成、行为与权限边界。" },
  { id: "system", title: "系统测试", color: "purple", description: "验证消息接入、网关协作、交付及自更新恢复；具体覆盖环节与就绪状态以测评集说明为准。" },
] as const;
export const SCORING_LABELS: Record<string, string> = { native: "原生评分 / 工程断言", native_geval: "原生评分 + 独立 GEval", geval: "LLM 判定 · GEval 主判" };
export const SOURCE_LABELS: Record<string, string> = { project: "项目自建", public_benchmark: "公开基准" };
export const PURPOSE_LABELS: Record<string, string> = { business_task: "业务任务完成检查", engineering_regression: "变更回归", benchmark: "能力摸底与配置对比" };
export const SCORER_ORIGINS: Record<string, string> = { official: "官方实现", project_adapter: "项目适配实现", llm_judge: "LLM Judge" };

export function subjectForRecord(record: EvaluationRecord) {
  const subject = asObject(record.benchmark).subject_type;
  return SUBJECTS.find(item => item.id === subject) ?? null;
}

export function catalogGroups(suites: EvaluationSuite[], subject: EvaluationSubject, capability = "") {
  const matching = suites.filter(suite => suite.subject_type === subject && (!capability || suite.capability_tags?.includes(capability)));
  return [{ id: "project", title: "项目测评" }, { id: "public_benchmark", title: "公开基准" }].map(group => ({
    ...group,
    suites: matching.filter(suite => suite.source_type === group.id && suite.implemented),
    planned: matching.filter(suite => suite.source_type === group.id && !suite.implemented),
  })).filter(group => group.suites.length || group.planned.length);
}

export function suiteFormKey(botId: string, suite: EvaluationSuite) {
  return JSON.stringify([botId, suite.subject_type, suite.suite_id, suite.benchmark?.case_set_hash]);
}
