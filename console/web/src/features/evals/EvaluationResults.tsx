import { useMemo, useState } from "react";
import { Alert, Button, Empty, Input, Select, Space, Table, Tag, Typography } from "@arco-design/web-react";
import type { EvaluationRecord, EvaluationTrial } from "./model";
import { normalizeTrial } from "./evaluationApi";
import { dateLabel, durationLabel, EXCLUSION_LABELS, OUTCOME_LABELS, rateLabel, revisionLabel, VERDICT_LABELS } from "./insightsModel";

const { Text, Title } = Typography;
const COLORS: Record<string, string> = { passed: "green", failed: "red", error: "orange", skipped: "gray" };

export function ResultOverview({ record }: { record: EvaluationRecord }) {
  const { insights } = record;
  if (!insights.counts) return <Empty description={record.status === "running" || record.status === "queued"
    ? "评测进行中，尚未生成结果摘要。" : "该评测未记录结果摘要。"} />;
  const verdict = VERDICT_LABELS[insights.verdict];
  return <div className="eval-result-overview">
    <Space wrap>
      <Text bold>结果摘要</Text>
      {verdict && <Tag color={insights.verdict === "passed" ? "green" : insights.verdict === "failed" ? "red" : "orange"}>{verdict}</Tag>}
      {!insights.complete && <Tag color="orange">{EXCLUSION_LABELS[insights.exclusion_reason] || "部分结果"}</Tag>}
    </Space>
    <div className="eval-outcome-metrics">
      <div><span>通过率</span><strong>{rateLabel(insights.pass_rate)}</strong></div>
      {Object.entries(OUTCOME_LABELS).map(([key, label]) => <div key={key} className={`eval-outcome-${key}`}>
        <span>{label}</span><strong>{insights.counts?.[key as keyof typeof insights.counts]}</strong>
      </div>)}
    </div>
    <div className="eval-outcome-bar" aria-label="测试结果分布">
      {Object.entries(insights.counts).map(([key, count]) => count > 0 && <span key={key} className={`eval-outcome-${key}`}
        style={{ flex: count }} title={`${OUTCOME_LABELS[key]} ${count}`} />)}
    </div>
    <Text type="secondary">已记录 {insights.observed ?? "—"} / 计划 {insights.planned ?? "—"} 次；通过率分母包含失败、异常和跳过。重复执行分别计数。</Text>
    {Object.keys(insights.capabilities).length > 0 && <div className="eval-capability-list">
      {Object.entries(insights.capabilities).map(([name, counts]) => <div key={name}>
        <Text>{name}</Text><Space size={8} wrap>{Object.entries(counts).map(([key, count]) => count > 0 && <Tag key={key} color={COLORS[key]}>{OUTCOME_LABELS[key]} {count}</Tag>)}</Space>
      </div>)}
    </div>}
  </div>;
}

function BoundedText({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false);
  const limit = 2400;
  return <div><pre className="eval-trial-text">{expanded ? value : value.slice(0, limit)}</pre>
    {value.length > limit && <button type="button" className="eval-text-action" onClick={() => setExpanded(!expanded)}>{expanded ? "收起正文" : `展开全文（${value.length} 字符）`}</button>}
  </div>;
}
function judgeReasons(trial: EvaluationTrial): string {
  const judge = trial.judge;
  if (!judge) return "";
  return ["reasons", "missing", "violations"].flatMap(key => {
    const value = judge[key];
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : typeof value === "string" ? [value] : [];
  }).join("\n");
}
export function EvaluationResults({ record }: { record: EvaluationRecord }) {
  const [outcome, setOutcome] = useState("all");
  const [search, setSearch] = useState("");
  const rows = useMemo(() => {
    const values = Array.isArray(record.result?.trials) ? record.result.trials : [];
    return values.map((raw, index) => {
      const trial = normalizeTrial(raw);
      const value = raw as Record<string, unknown>;
      return { ...trial, row_id: `${trial.trial_id}:${index}`, started_at: typeof value?.started_at === "string" ? value.started_at : null };
    });
  }, [record.result]);
  const filtered = rows.filter(trial => (outcome === "all" || trial.outcome === outcome)
    && `${trial.case_id} ${trial.case_ref} ${trial.dimension}`.toLowerCase().includes(search.toLowerCase()));
  return <Space direction="vertical" size={16} style={{ width: "100%" }}>
    <ResultOverview record={record} />
    {!!record.result && <section className="eval-case-results">
      <Title heading={6}>测试点结果</Title>
      <div className="eval-history-filters">
        <Select aria-label="测试点结果筛选" value={outcome} onChange={setOutcome} options={[
          { label: "全部结果", value: "all" }, ...Object.entries(OUTCOME_LABELS).map(([value, label]) => ({ label, value })),
        ]} />
        <Input aria-label="搜索测试点" placeholder="搜索测试点" value={search} onChange={setSearch} allowClear />
        <Text type="secondary">{filtered.length} 条</Text>
      </div>
      <Table rowKey="row_id" size="small" data={filtered} pagination={{ pageSize: 10 }} scroll={{ x: 680 }}
        expandProps={{ icon: ({ expanded, record: trial }) => <Button size="mini" type="text"
          aria-expanded={expanded} aria-label={`${expanded ? "收起" : "展开"}测试点 ${trial.case_id}`}>{expanded ? "−" : "+"}</Button> }}
        columns={[
          { title: "测试点", dataIndex: "case_id", width: 220, render: (_, row) => <span title={row.case_ref}>{row.case_id || row.case_ref || "标识未记录"}</span> },
          { title: "结果", dataIndex: "outcome", width: 85, render: value => <Tag color={COLORS[value] || "gray"}>{OUTCOME_LABELS[value] || "未知"}</Tag> },
          { title: "执行器 / 轮次", width: 135, render: (_, row) => `${row.target_id || "未记录"} / ${row.attempt || "—"}` },
          { title: "耗时", dataIndex: "duration_seconds", width: 95, render: durationLabel },
          { title: "开始时间", dataIndex: "started_at", width: 180, render: dateLabel },
        ]}
        expandedRowRender={row => <div className="eval-trial-detail">
          <Text type="secondary">{revisionLabel(record)} · {dateLabel(row.started_at)}</Text>
          {row.error && <Alert type="error" content={row.error} />}
          {judgeReasons(row) && <div><Text bold>判分记录</Text><BoundedText value={judgeReasons(row)} /></div>}
          <div><Text bold>Agent 回答</Text>{row.final_text ? <BoundedText value={row.final_text} /> : <Text type="secondary"> 未记录回答正文</Text>}</div>
          {row.stop_reason && <Text type="secondary">结束原因：{row.stop_reason}</Text>}
          <details><summary>原始测试记录</summary><BoundedText value={JSON.stringify(row, null, 2)} /></details>
        </div>} />
    </section>}
    {Object.keys(record.summary).length > 0 && <details className="eval-raw-summary"><summary>原始结果摘要</summary><BoundedText value={JSON.stringify(record.summary, null, 2)} /></details>}
  </Space>;
}
