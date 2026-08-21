import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Drawer, Empty, Message, Space, Spin, Tabs, Tag, Typography } from "@arco-design/web-react";
import BotCard from "../components/BotCard";
import ProvisionWizard from "../components/ProvisionWizard";
import JobsModal from "../features/bots/JobsModal";
import { useBotActions } from "../features/bots/useBotActions";
import { useBotsOverview } from "../features/bots/useBotsOverview";
import { useJobsModal } from "../features/bots/useJobsModal";
import BotTaskFlowPanel from "../features/bots/BotTaskFlowPanel";
import { api, streamLogs, streamTask } from "../api";
import type { BotInstance, BotStatus, Task } from "../types";
import { useEventStreamLines } from "../shared/hooks/useEventStreamLines";
import LogDrawer from "../shared/ui/LogDrawer";
import PageSection from "../shared/ui/PageSection";
import TaskStreamSheet from "../shared/ui/TaskStreamSheet";

interface Props {
  loadError: string | null;
  visible?: boolean;
}

const { Text, Title } = Typography;

function rosterState(status: BotStatus | undefined) {
  if (!status) return { label: "加载中", color: "gray" };
  if (status.active_state === "failed") return { label: "失败", color: "red" };
  if (!status.systemd_available) return { label: "systemd 不可用", color: "orange" };
  if (!status.registered) return { label: "未注册", color: "orange" };
  if (status.running && status.ws_connected === false) return { label: "连接异常", color: "orange" };
  if (status.running) return { label: "运行中", color: "green" };
  return { label: "已停止", color: "gray" };
}

function rosterAge(epoch: number | null | undefined) {
  if (!epoch) return "暂无任务";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - epoch));
  if (seconds < 60) return `${seconds}s 前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m 前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h 前`;
  return `${Math.floor(seconds / 86400)}d 前`;
}

export default function BotsPage({ loadError, visible = true }: Props) {
  const {
    bots,
    statuses,
    inventoryMap,
    activityMap,
    loading,
    runningBotCount,
    deployedBotCount,
    reloadBots,
    refreshStatuses,
  } = useBotsOverview(visible);
  const taskStream = useEventStreamLines();
  const logStream = useEventStreamLines();
  const jobsModal = useJobsModal();
  const [provisionBot, setProvisionBot] = useState<BotInstance | null>(null);
  const [taskResultStatus, setTaskResultStatus] = useState<Task["status"] | null>(null);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [selectedBotId, setSelectedBotId] = useState("");
  const activeTaskId = useRef<string | null>(null);

  useEffect(() => {
    if (bots.length === 0) {
      setSelectedBotId("");
      return;
    }
    if (!selectedBotId || !bots.some((bot) => bot.instance_id === selectedBotId)) {
      setSelectedBotId(bots[0].instance_id);
    }
  }, [bots, selectedBotId]);

  const selectedBot = bots.find((bot) => bot.instance_id === selectedBotId) ?? null;

  const openTask = useCallback(
    (
      bot: BotInstance,
      kind: string,
      task: Task,
      options?: { resolveFinalStatus?: boolean; onSuccess?: () => void },
    ) => {
      const resolveFinalStatus = options?.resolveFinalStatus ?? false;
      activeTaskId.current = task.id;
      setTaskResultStatus(resolveFinalStatus ? "running" : null);
      setTaskError(null);
      taskStream.start(
        (onLine, onStatus, onEnd) =>
          streamTask(
            task.id,
            onLine,
            () => {
              onEnd();
              void refreshStatuses(bots);
              if (!resolveFinalStatus) return;
              void api.task(task.id)
                .then((finished) => {
                  if (activeTaskId.current !== task.id) return;
                  setTaskResultStatus(finished.status);
                  if (finished.status === "done") {
                    options?.onSuccess?.();
                    Message.success(`${bot.display_name}：${kind}成功`);
                    taskStream.close();
                    return;
                  }
                  if (finished.status === "failed") {
                    const errorLines = finished.lines
                      .map((line) => line.trim())
                      .filter((line) => line.includes("[ERR]"));
                    const stageError = errorLines[errorLines.length - 1];
                    const causeError = errorLines
                      .slice(0, -1)
                      .reverse()
                      .find((line) => line !== stageError);
                    const detail = causeError && stageError
                      ? `${causeError}；${stageError}`
                      : stageError || `exit code ${finished.exit_code ?? "未知"}`;
                    setTaskError(detail);
                    Message.error(`${bot.display_name}：${kind}失败：${detail}`);
                  }
                })
                .catch((error) => {
                  if (activeTaskId.current !== task.id) return;
                  const detail = `无法读取任务最终状态：${
                    error instanceof Error ? error.message : String(error)
                  }`;
                  setTaskResultStatus("failed");
                  setTaskError(detail);
                  Message.error(`${bot.display_name}：${kind}失败：${detail}`);
                });
            },
            onStatus,
          ),
        { title: `${bot.display_name} · ${kind}`, running: true },
      );
    },
    [bots, refreshStatuses, taskStream],
  );

  const openApplyToolConfig = useCallback(
    (bot: BotInstance, task: Task, onSuccess: () => void) => {
      openTask(bot, "保存配置并重启", task, { resolveFinalStatus: true, onSuccess });
    },
    [openTask],
  );

  const closeTask = useCallback(() => {
    activeTaskId.current = null;
    taskStream.close();
  }, [taskStream]);

  const { isBusy, handleAction, handleRegister } = useBotActions({
    bots,
    refreshStatuses,
    openTask,
  });

  const openLogs = useCallback(
    (bot: BotInstance) => {
      logStream.start(
        (onLine, onStatus) => streamLogs(bot.instance_id, "cc", onLine, onStatus),
        { title: `${bot.display_name} · cc-connect 日志` },
      );
    },
    [logStream],
  );

  return (
    <>
      <PageSection
        title="机器人实例"
        description="以机器人为入口查看运行状态、能力配置与每条消息跨层流转证据。"
        extra={
          <>
            <Tag className="cc-status-tag" color="green">运行 {runningBotCount}</Tag>
            <Tag className="cc-status-tag" color="blue">已部署 {deployedBotCount}</Tag>
            <Tag className="cc-tag-meta">总计 {bots.length}</Tag>
          </>
        }
      >
        {loadError && (
          <Alert
            type="error"
            content={`无法连接后端：${loadError}。确认已在 WSL 启动 python -m console.backend。`}
            style={{ marginBottom: 16 }}
            showIcon
          />
        )}

        {loading ? (
          <Spin size={28} style={{ display: "block", marginTop: 80 }} />
        ) : bots.length === 0 ? (
          <Empty description="未发现机器人，检查仓库 bots/*/bot.yaml" style={{ marginTop: 80 }} />
        ) : (
          <div className="bot-instance-workspace">
            <aside className="bot-instance-roster" aria-label="机器人实例列表">
              <div className="bot-instance-roster-title">
                <span>实例</span>
                <Text type="secondary">{bots.length}</Text>
              </div>
              {bots.map((bot) => {
                const state = rosterState(statuses[bot.instance_id]);
                const activity = activityMap[bot.instance_id];
                return (
                  <button
                    key={bot.instance_id}
                    type="button"
                    className={`bot-instance-roster-item${selectedBotId === bot.instance_id ? " is-selected" : ""}`}
                    onClick={() => setSelectedBotId(bot.instance_id)}
                  >
                    <span className="bot-instance-roster-head">
                      <strong title={bot.display_name}>{bot.display_name}</strong>
                      <Tag size="small" color={state.color}>{state.label}</Tag>
                    </span>
                    <span className="bot-instance-roster-id">{bot.platform || "?"} · {bot.instance_id}</span>
                    <span className="bot-instance-roster-stats">
                      <span>活跃 <strong>{activity?.active_count ?? 0}</strong></span>
                      <span className={(activity?.failed_recent_count ?? 0) > 0 ? "has-failure" : ""}>
                        24h 失败 <strong>{activity?.failed_recent_count ?? 0}</strong>
                      </span>
                    </span>
                    <span className="bot-instance-roster-last">最近任务 {rosterAge(activity?.last_activity_at)}</span>
                  </button>
                );
              })}
            </aside>

            {selectedBot && (
              <section className="bot-instance-detail">
                <header className="bot-instance-detail-header">
                  <div>
                    <Space wrap size={8}>
                      <Title heading={4}>{selectedBot.display_name}</Title>
                      <Tag color={rosterState(statuses[selectedBot.instance_id]).color}>
                        {rosterState(statuses[selectedBot.instance_id]).label}
                      </Tag>
                      <Tag className="cc-tag-meta">{selectedBot.platform || "?"}</Tag>
                    </Space>
                    <Text type="secondary">
                      {selectedBot.instance_id} · MCP {inventoryMap[selectedBot.instance_id]?.mcp_services.length ?? "—"}
                      {" · "}工具包 {inventoryMap[selectedBot.instance_id]?.tool_packs.length ?? "—"}
                    </Text>
                  </div>
                  <Space wrap>
                    {!selectedBot.is_deployed ? (
                      <Button type="primary" onClick={() => setProvisionBot(selectedBot)}>首次部署</Button>
                    ) : (
                      <>
                        <Button
                          type="primary"
                          loading={isBusy(selectedBot.instance_id)}
                          onClick={() => void handleAction(selectedBot, statuses[selectedBot.instance_id]?.running ? "restart" : "start")}
                        >
                          {statuses[selectedBot.instance_id]?.running ? "重启" : "启动"}
                        </Button>
                        <Button onClick={() => openLogs(selectedBot)}>实时日志</Button>
                        <Button
                          loading={isBusy(selectedBot.instance_id)}
                          onClick={() => void handleAction(selectedBot, "update")}
                        >
                          更新实例
                        </Button>
                      </>
                    )}
                  </Space>
                </header>

                <Tabs type="line" defaultActiveTab="flow" className="bot-instance-tabs">
                  <Tabs.TabPane title="任务流" key="flow">
                    <BotTaskFlowPanel
                      bot={selectedBot}
                      visible={visible}
                      onOpenFullEvidence={() => jobsModal.show(selectedBot)}
                    />
                  </Tabs.TabPane>
                  <Tabs.TabPane title="运行与能力" key="capabilities">
                    <BotCard
                      bot={selectedBot}
                      status={statuses[selectedBot.instance_id]}
                      inventory={inventoryMap[selectedBot.instance_id]}
                      busy={isBusy(selectedBot.instance_id)}
                      onAction={(verb) => void handleAction(selectedBot, verb)}
                      onApplyToolConfig={(task, onSuccess) => openApplyToolConfig(selectedBot, task, onSuccess)}
                      onLogs={() => openLogs(selectedBot)}
                      onJobs={() => jobsModal.show(selectedBot)}
                      onProvision={() => setProvisionBot(selectedBot)}
                      onRegister={() => void handleRegister(selectedBot)}
                    />
                  </Tabs.TabPane>
                </Tabs>
              </section>
            )}
          </div>
        )}
      </PageSection>

      <TaskStreamSheet
        title={taskStream.title}
        visible={taskStream.open}
        running={taskStream.running}
        lines={taskStream.lines}
        onClose={closeTask}
        status={taskResultStatus ?? undefined}
        error={taskError}
        streamStatus={taskStream.status}
      />

      <LogDrawer
        title={logStream.title}
        visible={logStream.open}
        status={logStream.status}
        lines={logStream.lines}
        onClose={logStream.close}
      />

      <JobsModal
        visible={jobsModal.open}
        bot={jobsModal.bot}
        jobs={jobsModal.jobs}
        updatedAt={jobsModal.updatedAt}
        loading={jobsModal.loading}
        error={jobsModal.error}
        workspaceRoot={jobsModal.workspaceRoot}
        workspaceExists={jobsModal.workspaceExists}
        onRefresh={(bot, opts) => jobsModal.load(bot, opts)}
        onClose={jobsModal.close}
      />

      <ProvisionDrawer
        bot={provisionBot}
        onClose={() => setProvisionBot(null)}
        onChanged={() => {
          void reloadBots();
        }}
      />
    </>
  );
}

function ProvisionDrawer({
  bot,
  onClose,
  onChanged,
}: {
  bot: BotInstance | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  return (
    <Drawer
      title={bot ? `首次部署 · ${bot.display_name}` : "首次部署"}
      visible={!!bot}
      width={760}
      onCancel={onClose}
    >
      {bot && (
        <ProvisionWizard
          bot={bot}
          onClose={onClose}
          onChanged={onChanged}
        />
      )}
    </Drawer>
  );
}
