import { Button, Empty, Space, Tag, Typography } from "@arco-design/web-react";
import type { OverviewIssue } from "../../types";
import type { PageKey } from "../../components/Sidebar";
import { severityLabel, severityTagColor } from "../../shared/ui/status";

const { Text } = Typography;

interface Props {
  issues: OverviewIssue[];
  onNavigate: (page: PageKey) => void;
}

const SOURCE_LABEL: Record<OverviewIssue["source_type"], string> = {
  bot: "机器人",
  infra: "基础设施",
  task: "任务",
};

export default function IssueList({ issues, onNavigate }: Props) {
  if (issues.length === 0) {
    return (
      <Empty
        description="当前机器人、服务和控制台任务均无待处理问题。"
        className="overview-empty"
      />
    );
  }

  return (
    <div className="overview-issue-list">
      {issues.map((issue) => (
        <div key={issue.id} className={`overview-issue-row overview-issue-row-${issue.severity}`}>
          <div className="overview-issue-main">
            <Space size={8} wrap>
              <Tag className="cc-status-tag" color={severityTagColor(issue.severity)}>{severityLabel(issue.severity)}</Tag>
              <Tag className="cc-tag-meta">{SOURCE_LABEL[issue.source_type]}</Tag>
              <Text bold>{issue.source_name || issue.source_id}</Text>
            </Space>
            <div className="overview-issue-title">{issue.title}</div>
            {issue.detail && (
              <Text type="secondary" className="cc-text-small overview-issue-detail">
                {issue.detail}
              </Text>
            )}
          </div>
          <Button size="small" type="secondary" onClick={() => onNavigate(issue.target_page)}>
            {issue.action_label}
          </Button>
        </div>
      ))}
    </div>
  );
}
