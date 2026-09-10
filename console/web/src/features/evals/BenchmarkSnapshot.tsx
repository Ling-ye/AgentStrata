import { Descriptions, Space, Tag, Typography } from "@arco-design/web-react";
import type { EvaluationRecord } from "./model";
import { evaluationSuiteId } from "./insightsModel";
import { asObject, asText } from "./trialModel";
import { SCORING_LABELS, SOURCE_LABELS, PURPOSE_LABELS, SCORER_ORIGINS } from "./BenchmarkWorkbench";

export function BenchmarkSnapshot({ record, compact = false }: { record: EvaluationRecord; compact?: boolean }) {
  const snapshot = asObject(record.benchmark);
  const scoring = asObject(snapshot.scoring);
  const framework = [asText(scoring.framework), asText(scoring.framework_version)].filter(Boolean).join(" ");
  const label = asText(snapshot.name) || evaluationSuiteId(record) || "基准未记录";
  const judge = asObject(scoring.judge);
  if (compact) return <Space direction="vertical" size={2}><Typography.Text>{label}</Typography.Text>
    <Typography.Text type="secondary">{framework || "框架未记录"}</Typography.Text></Space>;
  return <section className="eval-benchmark-snapshot"><Space wrap><Typography.Text bold>本次测评配置</Typography.Text><Tag>{label}</Tag><Tag>{framework || "框架版本未记录"}</Tag></Space>
    <Descriptions size="small" column={1} data={[
      { label: "测评集来源 / 用途", value: `${SOURCE_LABELS[asText(snapshot.source_type)] || "未记录"} / ${PURPOSE_LABELS[asText(snapshot.purpose)] || "未记录"}` },
      { label: "数据版本 / 划分", value: `${asText(snapshot.data_version) || "未记录"} / ${asText(snapshot.split) || "未记录"}` },
      { label: "执行适配器", value: [asText(asObject(snapshot.executor).id), asText(asObject(snapshot.executor).driver), asText(snapshot.adapter_version)].filter(Boolean).join(" · ") || "未记录" },
      { label: "评分器来源 / 版本", value: `${SCORER_ORIGINS[asText(asObject(scoring.scorer).origin)] || "未记录"} / ${asText(asObject(scoring.scorer).version) || "未记录"}` },
      { label: "覆盖范围", value: asText(snapshot.coverage) || "未记录" },
      { label: "被测对象", value: asText(snapshot.target_scope) || "未记录" },
      { label: "题单摘要", value: asText(snapshot.case_set_hash) || "未记录" },
      { label: "评分方案", value: (scoring.mode === "geval" && scoring.primary !== "llm_judge" ? "历史自定义 GEval（非原生成绩）" : SCORING_LABELS[asText(scoring.mode)]) || "未记录" },
      { label: "原生评分", value: scoring.native === false ? "未启用" : asText(scoring.native_method) || "未记录" },
      { label: "语义方法", value: scoring.quality === false ? "未启用" : [asText(scoring.method), asText(scoring.implementation)].filter(Boolean).join(" · ") || "未记录" },
      { label: "评分模型", value: asText(judge.model) || "未记录 / 不适用" },
    ]} />
    <details><summary>评分模型参数与输入范围</summary><pre>{JSON.stringify({ judge: scoring.judge, parameters: scoring.parameters, input_scope: scoring.input_scope }, null, 2)}</pre></details>
    {!!Object.keys(asObject(scoring.rubric)).length && <details><summary>查看本次量表、步骤与阈值</summary><pre>{JSON.stringify(scoring.rubric, null, 2)}</pre></details>}
  </section>;
}
