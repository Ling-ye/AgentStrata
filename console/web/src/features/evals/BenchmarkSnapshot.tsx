import { Descriptions, Space, Tag, Typography } from "@arco-design/web-react";
import type { EvaluationRecord } from "./model";
import { evaluationSuiteId } from "./insightsModel";
import { asObject, asText } from "./trialModel";
import { SCORING_LABELS } from "./BenchmarkWorkbench";

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
      { label: "基准适配器", value: asText(snapshot.adapter_version) || "未记录" },
      { label: "覆盖范围", value: asText(snapshot.coverage) || "未记录" },
      { label: "被测对象", value: asText(snapshot.target_scope) || "未记录" },
      { label: "题单摘要", value: asText(snapshot.case_set_hash) || "未记录" },
      { label: "评分方案", value: SCORING_LABELS[asText(scoring.mode)] || "未记录" },
      { label: "原生评分", value: scoring.native === false ? "未启用" : asText(scoring.native_method) || "未记录" },
      { label: "语义方法", value: scoring.quality === false ? "未启用" : [asText(scoring.method), asText(scoring.implementation)].filter(Boolean).join(" · ") || "未记录" },
      { label: "评分模型", value: asText(judge.model) || "未记录 / 不适用" },
    ]} />
    {!!Object.keys(asObject(scoring.rubric)).length && <details><summary>查看本次量表、步骤与阈值</summary><pre>{JSON.stringify(scoring.rubric, null, 2)}</pre></details>}
  </section>;
}
