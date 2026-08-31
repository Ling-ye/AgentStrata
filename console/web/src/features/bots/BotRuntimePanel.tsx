import type { ReactNode } from "react";
import { Tag, Typography } from "@arco-design/web-react";
import type { BotInstance, BotStatus } from "../../types";

const { Text, Title } = Typography;

interface Props {
  bot: BotInstance;
  status?: BotStatus;
}

function logAge(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s 前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m 前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h 前`;
  return `${Math.floor(seconds / 86400)}d 前`;
}

function serviceState(status?: BotStatus): { label: string; color: string; tone: string } {
  if (!status) return { label: "加载中", color: "gray", tone: "muted" };
  if (status.active_state === "failed") return { label: "失败", color: "red", tone: "danger" };
  if (!status.systemd_available) return { label: "systemd 不可用", color: "orange", tone: "warning" };
  if (!status.registered) return { label: "未注册", color: "orange", tone: "warning" };
  if (status.running) return { label: "运行中", color: "green", tone: "success" };
  return { label: "已停止", color: "gray", tone: "muted" };
}

function connectionState(status?: BotStatus): { label: string; color: string; tone: string } {
  if (status?.channel_connected === true) return { label: "有连接证据", color: "green", tone: "success" };
  if (status?.channel_connected === false) return { label: "未见连接证据", color: "orange", tone: "warning" };
  return { label: "未观测", color: "gray", tone: "muted" };
}

function systemdState(status?: BotStatus): ReactNode {
  if (!status) return <Text type="secondary">—</Text>;
  if (!status.systemd_available) return <Tag size="small" color="orange">不可用</Tag>;
  if (!status.registered) return <Tag size="small" color="orange">未注册</Tag>;
  return <Tag size="small" color="green">已注册</Tag>;
}

function Metric({ label, value, tone = "muted" }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className={`bot-runtime-metric bot-runtime-metric-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default function BotRuntimePanel({ bot, status }: Props) {
  const service = serviceState(status);
  const connection = connectionState(status);
  const details: Array<{ label: string; value: ReactNode }> = [
    { label: "实例 ID", value: <Text code className="bot-runtime-code">{bot.instance_id}</Text> },
    { label: "PID", value: status?.pid ?? "—" },
    { label: "systemd", value: systemdState(status) },
    {
      label: "服务状态",
      value: status ? `${status.active_state || "—"} / ${status.sub_state || "—"}` : "—",
    },
    { label: "运行日志更新", value: logAge(status?.runtime_log_age_s) },
    { label: "运行拓扑", value: status?.main_process_kind || bot.runtime_kind },
    {
      label: "MainPID 身份",
      value: status?.main_process_verified == null
        ? "legacy / 未适用"
        : (status.main_process_verified ? "已验证 Gateway" : "不匹配"),
    },
    { label: "开机启用", value: status?.enabled || "—" },
    { label: "服务单元", value: <Text code className="bot-runtime-code">{status?.unit || bot.unit || "—"}</Text> },
    { label: "运行开始", value: status?.since || "—" },
  ];

  return (
    <div className="bot-runtime-panel">
      <section className="bot-runtime-section" aria-labelledby="bot-runtime-overview-title">
        <div className="bot-runtime-section-heading">
          <div>
            <Title id="bot-runtime-overview-title" heading={5}>运行概览</Title>
            <Text type="secondary">查看 Gateway 进程与 Channel 观测证据；不等同于真实 QQ/模型/客户端 E2E。</Text>
          </div>
          <Tag className="cc-status-tag" color={service.color}>{service.label}</Tag>
        </div>
        <div className="bot-runtime-metrics">
          <Metric
            label="服务"
            tone={service.tone}
            value={<Tag size="small" color={service.color}>{service.label}</Tag>}
          />
          <Metric
            label="Channel"
            tone={connection.tone}
            value={<Tag size="small" color={connection.color}>{connection.label}</Tag>}
          />
          <Metric
            label="错误"
            tone={(status?.error_count ?? 0) > 0 ? "danger" : "muted"}
            value={status?.error_count ?? "—"}
          />
        </div>
      </section>

      <section className="bot-runtime-section" aria-labelledby="bot-runtime-details-title">
        <div className="bot-runtime-section-heading">
          <div>
            <Title id="bot-runtime-details-title" heading={5}>实例信息</Title>
            <Text type="secondary">用于运行诊断的只读标识与 systemd 状态。</Text>
          </div>
        </div>
        <dl className="bot-runtime-details">
          {details.map((item) => (
            <div key={item.label}>
              <dt>{item.label}</dt>
              <dd>{item.value}</dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}
