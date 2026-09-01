import { Button, Card, Descriptions, Space, Tag, Tooltip, Typography } from "@arco-design/web-react";
import DoctorPanel from "./DoctorPanel";
import LoginPanel from "./LoginPanel";
import type { InfraService } from "../types";
import { healthTagColor, infraStateLabel } from "../shared/ui/status";

const { Text, Title } = Typography;

interface Props {
  service: InfraService;
  busy: boolean;
  onAction: (verb: string) => void;
  onLogs: () => void;
}

function stateTag(service: InfraService) {
  return <Tag className="cc-status-tag" color={healthTagColor(service.color)}>{infraStateLabel(service.state)}</Tag>;
}

function serviceTypeLabel(type: InfraService["service_type"]): string {
  if (type === "compose") return "共享 Docker";
  return "实例网关";
}

function formatUptime(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

const STATE_BORDER_COLOR: Record<string, string> = {
  green: "var(--cc-success)",
  yellow: "var(--cc-warning)",
  red: "var(--cc-danger)",
  grey: "var(--cc-text-tertiary)",
  gray: "var(--cc-text-tertiary)",
};

export default function ServiceCard({ service, busy, onAction, onLogs }: Props) {
  const running = service.state === "healthy" || service.state === "running" || service.state === "unhealthy";
  const isNotFound = service.state === "not_found";
  const borderColor = STATE_BORDER_COLOR[service.color] ?? STATE_BORDER_COLOR.grey;

  const infoItems: { label: string; value: React.ReactNode }[] = [];
  if (service.container) {
    infoItems.push({ label: "容器", value: <Text code style={{ fontSize: 11 }}>{service.container}</Text> });
  }
  if (service.uptime_s != null) {
    infoItems.push({ label: "运行时长", value: formatUptime(service.uptime_s) });
  }
  if (service.instance_id) {
    infoItems.push({ label: "实例", value: <Text code style={{ fontSize: 11 }}>{service.instance_id}</Text> });
  }
  if (service.env_configured !== undefined) {
    infoItems.push({
      label: "API Key",
      value: service.env_configured
        ? <Tag size="small" color="green">已配置</Tag>
        : <Tag size="small" color="orange">未配置</Tag>,
    });
  }

  return (
    <Card
      hoverable
      className="service-card"
      style={{ borderLeft: `4px solid ${borderColor}` }}
      title={
        <Space wrap>
          <Title heading={6} className="cc-card-title" title={service.display_name} style={{ margin: 0 }}>{service.display_name}</Title>
          {stateTag(service)}
        </Space>
      }
      extra={
        <Tag size="small" color="blue">{serviceTypeLabel(service.service_type)}</Tag>
      }
    >
      {infoItems.length > 0 && (
        <Descriptions size="small" column={2} data={infoItems} style={{ marginBottom: 10 }} />
      )}

      {isNotFound && (
        <Text type="secondary" className="cc-text-small" style={{ display: "block", marginBottom: 8 }}>
          容器不存在，请先执行 docker compose up -d
        </Text>
      )}

      <Space wrap style={{ marginTop: 4 }}>
        {running ? (
          <Button size="small" type="primary" loading={busy} onClick={() => onAction("restart")}>
            重启
          </Button>
        ) : (
          <Button size="small" type="primary" loading={busy} onClick={() => onAction("start")}>
            启动
          </Button>
        )}
        {running && (
          <Button size="small" status="danger" loading={busy} onClick={() => onAction("stop")}>
            停止
          </Button>
        )}
        {service.actions.includes("pull") && (
          <Tooltip content="拉取最新 Docker 镜像">
            <Button size="small" type="secondary" loading={busy} onClick={() => onAction("pull")}>拉取镜像</Button>
          </Tooltip>
        )}
        <Button size="small" type="secondary" onClick={onLogs}>日志</Button>
      </Space>

      <LoginPanel service={service} />
      <DoctorPanel service={service} />
    </Card>
  );
}
