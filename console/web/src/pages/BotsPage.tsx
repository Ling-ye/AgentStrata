import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Drawer, Empty, Message, Space, Spin, Select, Tabs, Tag, Tooltip, Typography } from "@arco-design/web-react";
import BotToolEditor from "../components/BotToolEditor";
import ProvisionWizard from "../components/ProvisionWizard";
import { useBotActions } from "../features/bots/useBotActions";
import { useBotsOverview } from "../features/bots/useBotsOverview";
import ObservationWorkbench from "../features/architecture/ObservationWorkbench";
import { botTabFromParams } from "../features/architecture/taskWorkspaceState";
import BotRuntimePanel from "../features/bots/BotRuntimePanel";
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

const { Title } = Typography;

function tabFromLocation() {
  return botTabFromParams(new URLSearchParams(window.location.hash.split("?")[1] ?? ""));
}

function rosterState(status: BotStatus | undefined) {
  if (!status) return { label: "加载中", color: "gray" };
  if (status.active_state === "failed") return { label: "失败", color: "red" };
  if (!status.systemd_available) return { label: "systemd 不可用", color: "orange" };
  if (!status.registered) return { label: "未注册", color: "orange" };
  if (status.running && status.channel_connected === false) return { label: "Channel 异常", color: "orange" };
  if (status.running) return { label: "运行中", color: "green" };
  return { label: "已停止", color: "gray" };
}

export default function BotsPage({ loadError, visible = true }: Props) {
  const {
    bots,
    statuses,
    inventoryMap,
    loading,
    runningBotCount,
    deployedBotCount,
    reloadBots,
    refreshStatuses,
  } = useBotsOverview(visible);
  const taskStream = useEventStreamLines();
  const logStream = useEventStreamLines();
  const [provisionBot, setProvisionBot] = useState<BotInstance | null>(null);
  const [taskResultStatus, setTaskResultStatus] = useState<Task["status"] | null>(null);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [selectedBotId, setSelectedBotId] = useState(() => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("instance") || "");
  const [activeTab, setActiveTab] = useState(tabFromLocation);
  const activeTaskId = useRef<string | null>(null);

  useEffect(() => {
    const navigate = () => {
      if (window.location.hash.split("?")[0] !== "#bots") return;
      const params = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
      const instance = params.get("instance");
      if (instance) setSelectedBotId(instance);
      const tab = botTabFromParams(params);
      setActiveTab(tab);
      if (params.get("tab") !== tab) {
        params.set("tab", tab);
        window.history.replaceState(null, "", "#bots?" + params);
      }
    };
    navigate();
    window.addEventListener("hashchange", navigate);
    return () => window.removeEventListener("hashchange", navigate);
  }, []);

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
  const selectedStatus = selectedBot ? statuses[selectedBot.instance_id] : undefined;
  const selectedInventory = selectedBot ? inventoryMap[selectedBot.instance_id] : undefined;

  const navigateTab = (tab: string) => {
    const params = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
    params.set("instance", selectedBotId);
    params.set("tab", tab);
    setActiveTab(botTabFromParams(params));
    if (tab !== "configuration") params.delete("entity");
    window.history.replaceState(null, "", "#bots?" + params);
  };

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
        (onLine, onStatus) => streamLogs(
          bot.instance_id,
          bot.runtime_kind === "gateway" ? "gateway" : "cc",
          onLine,
          onStatus,
        ),
        { title: `${bot.display_name} · ${bot.runtime_kind === "gateway" ? "Gateway" : "legacy edge"} 日志` },
      );
    },
    [logStream],
  );

  return (
    <>
      <PageSection
        title="机器人实例"
        description="实例运行、配置与任务记录。"
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
          <div className="bot-instance-workspace obs-instance-workspace">
            <div className="obs-instance-selector"><span>机器人实例</span><Select aria-label="选择机器人实例" value={selectedBotId} onChange={(id) => {
              setSelectedBotId(id);
              window.history.replaceState(null, "", "#bots?" + new URLSearchParams({ instance: id, tab: activeTab }));
            }}
              options={bots.map((bot) => ({ value: bot.instance_id, label: `${bot.display_name} · ${rosterState(statuses[bot.instance_id]).label}` }))} />
              <Tag>{bots.length} 个实例</Tag></div>

            {selectedBot && (
              <section className="bot-instance-detail">
                <header className="bot-instance-detail-header">
                  <div className="bot-instance-detail-summary">
                    <Space wrap size={8}>
                      <Title heading={4}>{selectedBot.display_name}</Title>
                      <Tag className="cc-status-tag" color={rosterState(selectedStatus).color}>
                        {rosterState(selectedStatus).label}
                      </Tag>
                      <Tag className="cc-tag-meta">{selectedBot.platform || "?"}</Tag>
                    </Space>
                  </div>
                  <div className="bot-instance-detail-actions">
                    {!selectedBot.is_deployed ? (
                      <Button type="primary" onClick={() => setProvisionBot(selectedBot)}>首次部署</Button>
                    ) : (
                      <Space wrap>
                        <Button
                          type="primary"
                          loading={isBusy(selectedBot.instance_id)}
                          onClick={() => void handleAction(selectedBot, selectedStatus?.running ? "restart" : "start")}
                        >
                          {selectedStatus?.running ? "重启" : "启动"}
                        </Button>
                        <Button
                          status="danger"
                          disabled={!selectedStatus?.running}
                          loading={isBusy(selectedBot.instance_id)}
                          onClick={() => void handleAction(selectedBot, "stop")}
                        >
                          停止
                        </Button>
                        {selectedStatus?.registered === false && (
                          <Tooltip content="注册 systemd 用户服务：安装模板 unit、启用 lingering 并写入实例配置。">
                            <Button
                              type="primary"
                              status="warning"
                              loading={isBusy(selectedBot.instance_id)}
                              onClick={() => void handleRegister(selectedBot)}
                            >
                              注册服务
                            </Button>
                          </Tooltip>
                        )}
                        <Tooltip content="同步代码和运行配置，按需重建依赖并重启实例。">
                          <Button
                            loading={isBusy(selectedBot.instance_id)}
                            onClick={() => void handleAction(selectedBot, "update")}
                          >
                            更新并重启
                          </Button>
                        </Tooltip>
                      </Space>
                    )}
                    <Button onClick={() => openLogs(selectedBot)}>服务日志</Button>
                  </div>
                </header>

                <Tabs type="line" activeTab={activeTab} onChange={navigateTab} className="bot-instance-tabs">
                  <Tabs.TabPane title="任务" key="tasks" />
                  <Tabs.TabPane title="分层配置" key="configuration" />
                  <Tabs.TabPane title="运行状态" key="runtime">
                    <BotRuntimePanel
                      bot={selectedBot}
                      status={selectedStatus}
                    />
                  </Tabs.TabPane>
                  <Tabs.TabPane title="能力与工具" key="capabilities">
                    <div className="bot-capability-panel">
                      <div className="bot-capability-heading">
                        <Title heading={5}>能力配置</Title>
                        <Button onClick={() => navigateTab("tasks")}>返回任务</Button>
                      </div>
                      <BotToolEditor
                        instanceId={selectedBot.instance_id}
                        isDeployed={selectedBot.is_deployed}
                        inventory={selectedInventory}
                        onApplyTask={(task, onSuccess) => openApplyToolConfig(selectedBot, task, onSuccess)}
                      />
                    </div>
                  </Tabs.TabPane>
                </Tabs>
                <ObservationWorkbench key={selectedBot.instance_id} bot={selectedBot}
                  view={activeTab === "configuration" ? "configuration" : "tasks"}
                  visible={visible && ["tasks", "configuration"].includes(activeTab)}
                  onEdit={() => navigateTab("capabilities")} />
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
