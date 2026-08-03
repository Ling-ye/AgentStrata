import { useCallback, useRef, useState } from "react";
import { Alert, Drawer, Empty, Message, Spin, Tag } from "@arco-design/web-react";
import BotCard from "../components/BotCard";
import ProvisionWizard from "../components/ProvisionWizard";
import JobsModal from "../features/bots/JobsModal";
import { useBotActions } from "../features/bots/useBotActions";
import { useBotsOverview } from "../features/bots/useBotsOverview";
import { useJobsModal } from "../features/bots/useJobsModal";
import { api, streamLogs, streamTask } from "../api";
import type { BotInstance, Task } from "../types";
import { useEventStreamLines } from "../shared/hooks/useEventStreamLines";
import LogDrawer from "../shared/ui/LogDrawer";
import PageSection from "../shared/ui/PageSection";
import TaskStreamSheet from "../shared/ui/TaskStreamSheet";

interface Props {
  loadError: string | null;
  visible?: boolean;
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
  const jobsModal = useJobsModal();
  const [provisionBot, setProvisionBot] = useState<BotInstance | null>(null);
  const [taskResultStatus, setTaskResultStatus] = useState<Task["status"] | null>(null);
  const [taskError, setTaskError] = useState<string | null>(null);
  const activeTaskId = useRef<string | null>(null);

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
        description="每个 BotSpec 独立部署、独立运行，卡片内可查看启用服务。"
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
          <div className="bot-grid">
            {bots.map((bot) => (
              <BotCard
                key={bot.instance_id}
                bot={bot}
                status={statuses[bot.instance_id]}
                inventory={inventoryMap[bot.instance_id]}
                busy={isBusy(bot.instance_id)}
                onAction={(verb) => void handleAction(bot, verb)}
                onApplyToolConfig={(task, onSuccess) => openApplyToolConfig(bot, task, onSuccess)}
                onLogs={() => openLogs(bot)}
                onJobs={() => jobsModal.show(bot)}
                onProvision={() => setProvisionBot(bot)}
                onRegister={() => void handleRegister(bot)}
              />
            ))}
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
        onRefresh={(bot, opts) => void jobsModal.load(bot, opts)}
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
