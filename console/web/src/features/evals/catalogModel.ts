import type { EvaluationRecord, EvaluationSubject, EvaluationSuite } from "./model";
import { asObject } from "./trialModel";

export const SUBJECTS = [
  { id: "model", title: "模型能力", color: "orange", description: "直接评估模型的回答与函数调用，不验证 Agent 编排和工具执行。" },
  { id: "agent", title: "Agent 能力", color: "arcoblue", description: "评估 Agent 在给定工具与上下文环境中完成任务的能力，不经过渠道接入与平台投递。" },
  { id: "system", title: "系统链路", color: "purple", description: "验证指定入口到终点之间的协作，只对实际经过并取得证据的环节作结论。" },
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
