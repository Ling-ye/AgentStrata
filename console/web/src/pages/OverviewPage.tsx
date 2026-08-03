import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Spin, Tag, Typography } from "@arco-design/web-react";
import { api } from "../api";
import IssueList from "../features/overview/IssueList";
import SummaryStrip from "../features/overview/SummaryStrip";
import PageSection from "../shared/ui/PageSection";
import { healthTagColor, infraStateLabel } from "../shared/ui/status";
import type { PageKey } from "../components/Sidebar";

const { Text } = Typography;

interface Props {
  onNavigate: (page: PageKey) => void;
  visible?: boolean;
}

export default function OverviewPage({ onNavigate, visible = true }: Props) {
  const overviewQuery = useQuery({
    queryKey: ["overview"],
    queryFn: api.overview,
    enabled: visible,
    refetchInterval: visible ? 15_000 : false,
  });
  const overview = overviewQuery.data ?? null;
  const error = overviewQuery.error instanceof Error ? overviewQuery.error.message : null;

  const generatedAt = useMemo(() => {
    if (!overview?.generated_at) return "";
    const date = new Date(overview.generated_at);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString();
  }, [overview?.generated_at]);

  return (
    <>
      <PageSection
        title="运维总览"
        description="集中查看机器人、基础服务和后台任务的当前健康状态。"
        extra={
          <>
            {overview && <Tag className="cc-status-tag" color={overview.summary.issues_critical > 0 ? "red" : "green"}>
              严重 {overview.summary.issues_critical}
            </Tag>}
            {generatedAt && <span className="infra-refresh-text">更新于 {generatedAt}</span>}
            <Button size="small" type="secondary" onClick={() => void overviewQuery.refetch()}>刷新</Button>
          </>
        }
      >
        {error && (
          <Alert
            type="error"
            content={`无法读取控制台总览：${error}`}
            className="block-gap-bottom"
            showIcon
          />
        )}
        {overviewQuery.isLoading && !overview ? (
          <OverviewLoading />
        ) : overview ? (
          <SummaryStrip summary={overview.summary} />
        ) : null}
      </PageSection>

      {overview && (
        <PageSection
          title="需要关注"
          description="按严重程度排序的可处理问题。"
          extra={<Tag>共 {overview.issues.length}</Tag>}
        >
          <IssueList issues={overview.issues} onNavigate={onNavigate} />
        </PageSection>
      )}

      {overview && (
        <PageSection
          title="运行面板"
          description="快速扫描核心对象，详细操作仍在机器人实例和服务管理页完成。"
        >
          <div className="overview-status-grid">
            <div className="overview-status-panel">
              <div className="overview-panel-title">机器人</div>
              {overview.bots.map((bot) => (
                <div key={bot.instance_id} className="overview-compact-row">
                  <div>
                    <Text bold>{bot.display_name}</Text>
                    <Text type="secondary" className="cc-text-small overview-row-subtitle">
                      {bot.instance_id}
                    </Text>
                  </div>
                  <Tag className="cc-status-tag" color={healthTagColor(bot.health_color)}>
                    {bot.health_label}
                  </Tag>
                </div>
              ))}
            </div>

            <div className="overview-status-panel">
              <div className="overview-panel-title">基础设施</div>
              {overview.infra.map((service) => (
                <div key={service.id} className="overview-compact-row">
                  <div>
                    <Text bold>{service.display_name}</Text>
                    <Text type="secondary" className="cc-text-small overview-row-subtitle">
                      {service.service_type === "compose" ? "共享 Docker" : "实例网关"}
                    </Text>
                  </div>
                  <Tag className="cc-status-tag" color={healthTagColor(service.color)}>
                    {infraStateLabel(service.state)}
                  </Tag>
                </div>
              ))}
            </div>
          </div>
        </PageSection>
      )}
    </>
  );
}

function OverviewLoading() {
  return (
    <div className="overview-summary-grid">
      {["机器人", "基础设施", "任务", "失败", "关注"].map((label) => (
        <div key={label} className="overview-metric">
          <Text type="secondary" className="cc-text-small">{label}</Text>
          <div className="overview-skeleton-bar overview-skeleton-value" />
          <div className="overview-skeleton-bar" />
        </div>
      ))}
      <Spin size={20} className="overview-inline-spinner" />
    </div>
  );
}
