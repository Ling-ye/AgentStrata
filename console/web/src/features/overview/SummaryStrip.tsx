import { Tag, Typography } from "@arco-design/web-react";
import type { OverviewSummary } from "../../types";

const { Text } = Typography;

interface Props {
  summary: OverviewSummary;
}

const ITEMS = [
  { key: "bots", label: "机器人", value: (s: OverviewSummary) => `${s.bots_running}/${s.bots_total}`, hint: "运行中 / 总数" },
  { key: "infra", label: "基础设施", value: (s: OverviewSummary) => `${s.infra_healthy}/${s.infra_total}`, hint: "健康 / 总数" },
  { key: "tasks", label: "后台任务", value: (s: OverviewSummary) => String(s.tasks_running), hint: "运行中" },
  { key: "failed", label: "近期失败", value: (s: OverviewSummary) => String(s.tasks_failed_recent), hint: "需排查" },
];

export default function SummaryStrip({ summary }: Props) {
  return (
    <div className="overview-summary-grid">
      {ITEMS.map((item) => (
        <div key={item.key} className="overview-metric">
          <Text className="cc-text-small overview-metric-label">{item.label}</Text>
          <div className="overview-metric-value">{item.value(summary)}</div>
          <Text className="cc-text-small overview-metric-hint">{item.hint}</Text>
        </div>
      ))}
      <div className="overview-metric overview-metric-attention">
        <Text className="cc-text-small overview-metric-label">需要关注</Text>
        <div className="overview-metric-value">
          {summary.issues_critical + summary.issues_warning}
        </div>
        <div className="overview-metric-tags">
          <Tag className="cc-status-tag" color={summary.issues_critical > 0 ? "red" : "gray"}>严重 {summary.issues_critical}</Tag>
          <Tag className="cc-status-tag" color={summary.issues_warning > 0 ? "orange" : "gray"}>警告 {summary.issues_warning}</Tag>
        </div>
      </div>
    </div>
  );
}
