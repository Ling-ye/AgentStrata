import { Button, Card, Descriptions, Space, Tabs, Tag, Tooltip, Typography } from "@arco-design/web-react";
import BotToolEditor from "./BotToolEditor";
import type { BotInstance, BotInventory, BotStatus, Task } from "../types";

const { Text, Title } = Typography;

interface Props {
  bot: BotInstance;
  status?: BotStatus;
  inventory?: BotInventory;
  busy: boolean;
  onAction: (verb: "start" | "stop" | "restart" | "update" | "dump") => void;
  onApplyToolConfig?: (task: Task, onSuccess: () => void) => void;
  onLogs: () => void;
  onJobs: () => void;
  onProvision: () => void;
  onRegister: () => void;
}

function stateTag(status?: BotStatus) {
  if (!status) return <Tag className="cc-status-tag" color="gray">加载中</Tag>;
  if (!status.systemd_available) return <Tag className="cc-status-tag" color="orange">systemd 不可用</Tag>;
  if (!status.registered) return <Tag className="cc-status-tag" color="orange">未注册</Tag>;
  if (status.running) return <Tag className="cc-status-tag" color="green">运行中</Tag>;
  if (status.active_state === "failed") return <Tag className="cc-status-tag" color="red">失败</Tag>;
  return <Tag className="cc-status-tag" color="gray">已停止</Tag>;
}

function botCardTone(status?: BotStatus): string {
  if (!status) return "bot-card-muted";
  if (status.active_state === "failed") return "bot-card-danger";
  if (!status.systemd_available || !status.registered || status.ws_connected === false) return "bot-card-warning";
  if (status.running) return "bot-card-running";
  return "bot-card-muted";
}

function age(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s 前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m 前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h 前`;
  return `${Math.floor(seconds / 86400)}d 前`;
}

function inventorySummary(inv?: BotInventory): React.ReactNode {
  if (!inv) return null;
  const mcpCount = inv.mcp_services.length;
  const packCount = inv.tool_packs.length;
  if (mcpCount === 0 && packCount === 0) return <Tag size="small" color="gray">未绑定</Tag>;
  return (
    <Space size={4} wrap>
      {mcpCount > 0 && <Tag size="small" color="blue">MCP {mcpCount}</Tag>}
      {packCount > 0 && <Tag size="small" color="blue">工具包 {packCount}</Tag>}
    </Space>
  );
}

export default function BotCard({ bot, status, inventory, busy, onAction, onApplyToolConfig, onLogs, onJobs, onProvision, onRegister }: Props) {
  const running = status?.running ?? false;
  const ws = status?.ws_connected;
  const shouldShowRegister = status?.registered === false;

  return (
    <Card
      className={`bot-card ${botCardTone(status)}`}
      hoverable
      title={
        <Space wrap>
          <Title heading={5} className="cc-card-title" title={bot.display_name} style={{ margin: 0 }}>
            {bot.display_name}
          </Title>
          {stateTag(status)}
        </Space>
      }
      extra={
        <Space wrap>
          <Tag size="small" className="cc-tag-meta" title={bot.platform || "?"}>{bot.platform || "?"}</Tag>
          {inventorySummary(inventory)}
        </Space>
      }
    >
      <Tabs type="capsule" size="small">
        <Tabs.TabPane title="运行状态" key="status">
          <Descriptions
            size="small"
            column={2}
            data={[
              { label: "实例", value: <Text code>{bot.instance_id}</Text> },
              { label: "PID", value: status?.pid ?? "—" },
              {
                label: "长连接",
                value:
                  ws == null ? (
                    <Text type="secondary">—</Text>
                  ) : ws ? (
                    <Tag size="small" color="green">已连接</Tag>
                  ) : (
                    <Tag size="small" color="orange">未连接</Tag>
                  ),
              },
              { label: "日志更新", value: age(status?.cc_log_age_s ?? null) },
              {
                label: "错误",
                value:
                  status && status.error_count > 0 ? (
                    <Text type="error">{status.error_count}</Text>
                  ) : (
                    <Text type="secondary">0</Text>
                  ),
              },
              { label: "今日提问", value: status?.questions_today ?? "—" },
            ]}
          />

          {!bot.is_deployed && (
            <div className="card-action-row">
              <Text type="warning" className="cc-text-small">
                尚未部署（缺 {bot.wsl_home}）。
              </Text>
              <div className="card-action-row-compact">
                <Button type="primary" onClick={onProvision}>
                  首次部署
                </Button>
              </div>
            </div>
          )}

          {bot.is_deployed && (
            <div className="card-action-row">
              <Space wrap>
                {running ? (
                  <Button type="primary" loading={busy} onClick={() => onAction("restart")}>
                    重启
                  </Button>
                ) : (
                  <Button type="primary" loading={busy} onClick={() => onAction("start")}>
                    启动
                  </Button>
                )}
                <Button status="danger" disabled={!running} loading={busy} onClick={() => onAction("stop")}>
                  停止
                </Button>
                {shouldShowRegister && (
                  <Tooltip content="注册 systemd 用户服务：装模板 unit + 开 lingering + 写本实例 conf，启停重启依赖它">
                    <Button type="primary" status="warning" loading={busy} onClick={onRegister}>
                      注册服务
                    </Button>
                  </Tooltip>
                )}
                <Tooltip content="生成运行时 env、同步代码，按需重建依赖并重启该实例">
                  <Button type="secondary" loading={busy} onClick={() => onAction("update")}>
                    更新并重启
                  </Button>
                </Tooltip>
                <Button type="secondary" onClick={onLogs}>日志</Button>
                <Button type="secondary" onClick={onJobs}>任务</Button>
                <Tooltip content="抓一份诊断快照（dump.sh）">
                  <Button type="secondary" loading={busy} onClick={() => onAction("dump")}>
                    诊断
                  </Button>
                </Tooltip>
              </Space>
            </div>
          )}
        </Tabs.TabPane>
        <Tabs.TabPane title="能力与工具" key="tools">
          <BotToolEditor
            instanceId={bot.instance_id}
            isDeployed={bot.is_deployed}
            inventory={inventory}
            onApplyTask={onApplyToolConfig}
          />
        </Tabs.TabPane>
      </Tabs>
    </Card>
  );
}
