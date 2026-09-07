import { useMemo, useState } from "react";
import { Alert, Button, Empty, Select, Space, Table, Tag, Typography } from "@arco-design/web-react";
import type { EvaluationRecord } from "./model";
import { dateLabel, durationLabel, evaluationSuiteId, EXCLUSION_LABELS, rateLabel, recordTime, revisionLabel, trendSeries, versionChanges } from "./insightsModel";

const { Text } = Typography;
const SUITES = [
  { value: "agentstrata-capabilities-v1", label: "Agent 能力" },
  { value: "agentstrata-qq-message-flow-v1", label: "QQ 链路" },
];
export default function EvaluationTrends({ records, onOpen }: { records: EvaluationRecord[]; onOpen: (record: EvaluationRecord) => void }) {
  const [suite, setSuite] = useState(SUITES[0].value);
  const [days, setDays] = useState(30);
  const [seriesKey, setSeriesKey] = useState("");
  const [metric, setMetric] = useState("pass_rate");
  const [focusedId, setFocusedId] = useState("");
  const scoped = useMemo(() => records.filter(record => evaluationSuiteId(record) === suite
    && (!days || recordTime(record) >= Date.now() - days * 86400000)), [records, suite, days]);
  const series = useMemo(() => trendSeries(scoped), [scoped]);
  const activeSeries = series.find(item => item.key === seriesKey) ?? series[0];
  const points = activeSeries?.records ?? [];
  const focused = points.find(record => record.evaluation_id === focusedId) ?? points[points.length - 1];
  const valueOf = (record: EvaluationRecord) => metric === "pass_rate" ? record.insights.pass_rate === null ? null : record.insights.pass_rate * 100 : record.duration_seconds;
  const chartPoints = points.slice(-200);
  const values = chartPoints.map(valueOf).filter((value): value is number => value !== null && value >= 0);
  const max = metric === "pass_rate" ? 100 : Math.max(1, ...values) * 1.1;
  const start = chartPoints.length ? recordTime(chartPoints[0]) : 0;
  const end = chartPoints.length ? recordTime(chartPoints[chartPoints.length - 1]) : 0;
  const x = (record: EvaluationRecord) => end === start ? 450 : 55 + (recordTime(record) - start) / (end - start) * 795;
  const y = (value: number) => 210 - value / max * 180;
  const segments: string[] = [];
  let path = "";
  for (const record of chartPoints) {
    const value = valueOf(record);
    if (value === null) { if (path) segments.push(path); path = ""; continue; }
    path += `${path ? " L" : "M"}${x(record)},${y(value)}`;
  }
  if (path) segments.push(path);
  const excluded = scoped.filter(record => !record.insights.trend_eligible || !Number.isFinite(recordTime(record)));
  const first = points[0], last = points[points.length - 1];
  const change = first && last && first.insights.pass_rate !== null && last.insights.pass_rate !== null
    ? (last.insights.pass_rate - first.insights.pass_rate) * 100 : null;
  return <div className="eval-trends">
    <div className="eval-history-filters">
      <Select aria-label="趋势测试方向" value={suite} onChange={value => { setSuite(value); setSeriesKey(""); }} options={SUITES} />
      <Select aria-label="趋势时间范围" value={days} onChange={setDays} options={[
        { value: 7, label: "最近 7 天" }, { value: 30, label: "最近 30 天" }, { value: 90, label: "最近 90 天" }, { value: 0, label: "全部时间" },
      ]} />
      <Select aria-label="趋势测试条件" className="eval-series-select" value={activeSeries?.key} placeholder="暂无可用测试条件" onChange={setSeriesKey}
        options={series.map(item => ({ value: item.key, label: item.label }))} />
      <Select aria-label="趋势指标" value={metric} onChange={setMetric} options={[{ value: "pass_rate", label: "通过率" }, { value: "duration", label: "执行耗时" }]} />
    </div>
    {points.length ? <>
      <div className="eval-trend-totals">
        <div><span>完整评测</span><strong>{points.length} 次</strong></div>
        <div><span>最近通过率</span><strong>{rateLabel(last.insights.pass_rate)}</strong></div>
        <div><span>较本范围首条</span><strong>{points.length > 1 && change !== null ? `${change > 0 ? "+" : ""}${change.toFixed(1)} 个百分点` : "—"}</strong></div>
      </div>
      <div className="eval-trend-chart">
        <svg viewBox="0 0 900 250" role="group" aria-label={metric === "pass_rate" ? "评测通过率变化曲线" : "评测耗时变化曲线"}>
          {[0, 0.25, 0.5, 0.75, 1].map(ratio => <g key={ratio}>
            <line x1="55" x2="850" y1={y(ratio * max)} y2={y(ratio * max)} className="eval-chart-grid" />
            <text x="45" y={y(ratio * max) + 4} textAnchor="end">{metric === "pass_rate" ? `${ratio * 100}%` : `${(ratio * max).toFixed(0)}s`}</text>
          </g>)}
          {segments.map((d, index) => <path d={d} key={index} className="eval-chart-line" />)}
          {chartPoints.map(record => {
            const value = valueOf(record); if (value === null) return null;
            const label = `${dateLabel(record.started_at || record.created_at)}，${revisionLabel(record)}，${metric === "pass_rate" ? rateLabel(record.insights.pass_rate) : durationLabel(record.duration_seconds)}`;
            return <circle key={record.evaluation_id} cx={x(record)} cy={y(value)} r={focused?.evaluation_id === record.evaluation_id ? 7 : 5}
              className="eval-chart-point" role="button" tabIndex={0} aria-label={`查看评测 ${record.evaluation_id}，${label}`}
              onMouseEnter={() => setFocusedId(record.evaluation_id)} onFocus={() => setFocusedId(record.evaluation_id)}
              onClick={() => onOpen(record)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpen(record); } }}>
              <title>{label}</title>
            </circle>;
          })}
          <text x="55" y="238">{new Date(start).toLocaleDateString("zh-CN")}</text>
          <text x="850" y="238" textAnchor="end">{new Date(end).toLocaleDateString("zh-CN")}</text>
        </svg>
        {focused && <div className="eval-trend-point-detail">
          <Space wrap><Text>{dateLabel(focused.started_at || focused.created_at)}</Text><Tag>{revisionLabel(focused)}</Tag>
            <Text>通过 {focused.insights.counts?.passed} / {focused.insights.observed} · {rateLabel(focused.insights.pass_rate)}</Text>
            <Button size="small" type="text" onClick={() => onOpen(focused)}>查看本次评测</Button></Space>
        </div>}
      </div>
      <Text type="secondary">同一测试条件内按执行时间排列，{points.length > 200 ? "曲线显示最近 200 个点，完整记录见下表。" : "每个点对应一次完整评测。"}代码和配置变化见记录标记；模型提供方更新及外部数据变化仍可能影响结果。</Text>
      <Table rowKey="evaluation_id" size="small" data={[...points].reverse()} pagination={{ pageSize: 10 }} scroll={{ x: 1050 }} columns={[
        { title: "评测时间", width: 190, render: (_, record) => <Button type="text" size="small" onClick={() => onOpen(record)}>{dateLabel(record.started_at || record.created_at)}</Button> },
        { title: "通过率", width: 100, render: (_, record) => rateLabel(record.insights.pass_rate) },
        { title: "通过 / 全部", width: 115, render: (_, record) => `${record.insights.counts?.passed ?? "—"} / ${record.insights.observed ?? "—"}` },
        { title: "Git 版本", width: 220, render: (_, record) => <span title={record.source_revision.commit ?? ""}>{revisionLabel(record)}</span> },
        { title: "配置 / 实现", width: 140, render: (_, record) => <code title={record.insights.configuration_fingerprint ?? ""}>{record.insights.configuration_fingerprint?.slice(0, 10) || "未记录"}</code> },
        { title: "耗时", width: 110, render: (_, record) => durationLabel(record.duration_seconds) },
        { title: "变化", width: 230, render: (_, record) => versionChanges(points[points.indexOf(record) - 1], record).join(" · ") || "—" },
      ]} />
    </> : <Empty description="该范围内没有可绘制的完整评测。完成相同测试条件的评测后，这里会记录变化。" />}
    {excluded.length > 0 && <details className="eval-excluded"><summary>{excluded.length} 条记录未进入曲线</summary>
      {excluded.map(record => <div key={record.evaluation_id}><Button type="text" size="small" onClick={() => onOpen(record)}>{dateLabel(record.created_at)}</Button>
        <Text>{EXCLUSION_LABELS[record.insights.exclusion_reason] || "缺少有效时间"} · {record.evaluation_id}</Text></div>)}
    </details>}
    {points.length === 1 && <Alert type="info" content="当前条件只有一次完整评测；再次执行相同测试后即可观察变化。" />}
  </div>;
}
