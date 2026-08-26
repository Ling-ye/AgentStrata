import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Message,
  Modal,
  Skeleton,
  Space,
  Spin,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import type {
  BotInstance,
  BotTask,
  TasksResponse,
  TaskFlowEvidenceLevel,
  TaskFlowTransition,
} from "../../types";
import { fmtTime, jobStatusColor } from "./jobsFormat";
import TaskEvidencePanel from "./TaskEvidencePanel";
import {
  buildFlowRows,
  groupTasks,
  isLiveTask,
  nextTaskIdAfterDelete,
  shouldRefreshTerminalFlow,
  taskDeleteAvailability,
  taskStatusLabel,
  withoutTaskRecord,
} from "./taskFlowModel";

const { Text, Title } = Typography;

interface Props {
  bot: BotInstance;
  visible?: boolean;
}

const EVIDENCE_META: Record<
  TaskFlowEvidenceLevel,
  { label: string; color: string; note: string }
> = {
  observed: { label: "已观测", color: "green", note: "来自当前任务的直接运行时证据" },
  correlated: { label: "摘要关联", color: "blue", note: "仅用于观测，不参与授权" },
  declared: { label: "静态声明", color: "gray", note: "来自实例拓扑，不代表本轮实际执行" },
  provider_opaque: { label: "Provider 不透明", color: "purple", note: "AgentStrata 无法读取内部状态" },
  missing: { label: "缺少证据", color: "orange", note: "不会根据相邻步骤推断" },
};

export default function BotTaskFlowPanel({ bot, visible = true }: Props) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const [deletingId, setDeletingId] = useState("");
  const previousFlowTaskRef = useRef<{ key: string; status: string } | null>(null);
  const tasksQuery = useQuery({
    queryKey: ["bot-tasks", bot.instance_id],
    queryFn: () => api.tasks(bot.instance_id),
    enabled: visible,
    refetchInterval: visible ? 5_000 : false,
  });
  const tasks = tasksQuery.data?.tasks ?? [];

  useEffect(() => {
    setQuery("");
    setSelectedId("");
  }, [bot.instance_id]);

  useEffect(() => {
    if (tasks.length === 0) {
      setSelectedId("");
      return;
    }
    if (!selectedId || !tasks.some((task) => task.task_id === selectedId)) {
      setSelectedId(tasks[0].task_id);
    }
  }, [selectedId, tasks]);

  const selectedTask = tasks.find((task) => task.task_id === selectedId) ?? null;
  const flowQuery = useQuery({
    queryKey: ["bot-task-flow", bot.instance_id, selectedId],
    queryFn: () => api.taskFlow(bot.instance_id, selectedId),
    enabled: visible && !!selectedId,
    refetchInterval: visible && selectedTask && isLiveTask(selectedTask.status) ? 2_500 : false,
  });
  useEffect(() => {
    if (!visible) return;
    const current = selectedTask
      ? { key: `${bot.instance_id}:${selectedTask.task_id}`, status: selectedTask.status }
      : null;
    const previous = previousFlowTaskRef.current;
    previousFlowTaskRef.current = current;
    if (!shouldRefreshTerminalFlow(previous, current) || !selectedTask) return;
    void queryClient.invalidateQueries({
      queryKey: ["bot-task-flow", bot.instance_id, selectedTask.task_id],
      exact: true,
      refetchType: "active",
    });
  }, [bot.instance_id, queryClient, selectedTask?.task_id, selectedTask?.status, visible]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return tasks;
    return tasks.filter((task) =>
      [task.task_id, task.description, task.progress, task.current_step, task.primary_model]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase()
        .includes(needle),
    );
  }, [query, tasks]);
  const groups = useMemo(() => groupTasks(filtered), [filtered]);
  const rows = useMemo(
    () => buildFlowRows(flowQuery.data?.transitions ?? []),
    [flowQuery.data?.transitions],
  );

  const confirmDeleteTask = (task: BotTask) => {
    const availability = taskDeleteAvailability(task.status);
    if (!availability.allowed) return;
    Modal.confirm({
      title: "删除任务记录",
      content: `永久删除“${task.description || task.task_id}”的任务详情、上下文快照和原始事件？关联后台 Job 不会被删除。`,
      okText: "删除记录",
      cancelText: "取消",
      okButtonProps: { status: "danger" },
      onOk: async () => {
        setDeletingId(task.task_id);
        try {
          await api.deleteTask(bot.instance_id, task.task_id);
          const taskQueryKey = ["bot-tasks", bot.instance_id];
          queryClient.setQueryData<TasksResponse>(
            taskQueryKey,
            (current) => current ? withoutTaskRecord(current, task.task_id) : current,
          );
          queryClient.removeQueries({
            queryKey: ["bot-task-flow", bot.instance_id, task.task_id],
            exact: true,
          });
          if (selectedId === task.task_id) {
            setSelectedId(nextTaskIdAfterDelete(tasks, task.task_id));
          }
          Message.success("任务记录已删除");
          try {
            await queryClient.invalidateQueries({ queryKey: taskQueryKey, exact: true });
          } catch {
            Message.warning("任务记录已删除，但列表刷新失败，请手动刷新。");
          }
        } catch (error) {
          Message.error(error instanceof Error ? error.message : String(error));
        } finally {
          setDeletingId("");
        }
      },
    });
  };

  if (tasksQuery.isLoading) {
    return <div className="bot-flow-loading"><Spin size={28} /></div>;
  }
  if (tasksQuery.error) {
    return (
      <Alert
        type="error"
        showIcon
        content={`任务列表加载失败：${tasksQuery.error instanceof Error ? tasksQuery.error.message : String(tasksQuery.error)}`}
      />
    );
  }
  if (tasks.length === 0) {
    return (
      <div className="bot-flow-empty">
        <Empty description="该实例还没有可观察任务" />
        <Text type="secondary">消息进入 ACP 并成功创建任务记录后，跨层链路会显示在这里。</Text>
      </div>
    );
  }

  return (
    <div className="bot-flow-workbench">
      <aside className="bot-flow-task-list">
        <div className="bot-flow-task-tools">
          <Input.Search
            allowClear
            value={query}
            onChange={setQuery}
            placeholder="搜索任务、模型或进度"
          />
          <Button size="small" loading={tasksQuery.isFetching} onClick={() => void tasksQuery.refetch()}>
            刷新
          </Button>
        </div>
        <div className="bot-flow-task-summary">
          <span>活跃 <strong>{tasksQuery.data?.summary.active_count ?? 0}</strong></span>
          <span>24h 失败 <strong>{tasksQuery.data?.summary.failed_recent_count ?? 0}</strong></span>
          <span>总任务 <strong>{tasksQuery.data?.total_count ?? tasks.length}</strong></span>
        </div>
        <div className="bot-flow-task-scroll">
          {groups.map((group) => group.tasks.length > 0 && (
            <section key={group.key} className="bot-flow-task-group">
              <div className="bot-flow-task-group-title">
                <span>{group.label}</span><span>{group.tasks.length}</span>
              </div>
              {group.tasks.map((task) => {
                const deletion = taskDeleteAvailability(task.status);
                return (
                  <div
                    key={task.task_id}
                    className={`bot-flow-task-entry${task.task_id === selectedId ? " is-selected" : ""}`}
                  >
                    <button
                      type="button"
                      className="bot-flow-task-item"
                      onClick={() => setSelectedId(task.task_id)}
                    >
                      <span className="bot-flow-task-item-head">
                        <strong>{task.description || "（无文本摘要）"}</strong>
                        <Tag size="small" color={jobStatusColor(task.status)}>{taskStatusLabel(task.status)}</Tag>
                      </span>
                      <span className="bot-flow-task-progress">{task.current_step || task.progress || "等待运行时事件"}</span>
                      <span className="bot-flow-task-meta">
                        <span>{fmtTime(task.sort_time)}</span>
                        <span>{task.primary_model || "未记录模型"}</span>
                      </span>
                    </button>
                    <span className="bot-flow-task-delete" title={deletion.reason}>
                      <Button
                        type="text"
                        size="mini"
                        status="danger"
                        disabled={!deletion.allowed}
                        loading={deletingId === task.task_id}
                        aria-label={`删除任务 ${task.description || task.task_id}`}
                        onClick={() => confirmDeleteTask(task)}
                      >
                        删除
                      </Button>
                    </span>
                  </div>
                );
              })}
            </section>
          ))}
        </div>
      </aside>

      <main className="bot-flow-detail">
        {selectedTask && (
          <header className="bot-flow-detail-header">
            <div>
              <Space size={8} wrap>
                <Tag color={jobStatusColor(selectedTask.status)}>{taskStatusLabel(selectedTask.status)}</Tag>
                <Text code>{selectedTask.task_id}</Text>
                {flowQuery.isFetching && <Text type="secondary">更新中</Text>}
              </Space>
              <Title heading={5}>{selectedTask.description || "（无文本摘要）"}</Title>
              <Text type="secondary">{selectedTask.progress || "暂无任务进度"}</Text>
            </div>
            <Space wrap>
              <Button type="primary" loading={flowQuery.isFetching} onClick={() => void flowQuery.refetch()}>
                刷新链路
              </Button>
            </Space>
          </header>
        )}

        {flowQuery.isLoading ? (
          <Skeleton text={{ rows: 10 }} animation />
        ) : flowQuery.error ? (
          <Alert
            type="error"
            showIcon
            content={`任务流加载失败：${flowQuery.error instanceof Error ? flowQuery.error.message : String(flowQuery.error)}`}
          />
        ) : flowQuery.data ? (
          <>
            <section className="bot-flow-section">
              <div className="bot-flow-section-title">
                <span>分层链路</span>
                <Space size={6} wrap>
                  <Tag color="green">直接证据 {flowQuery.data.coverage.observed}</Tag>
                  <Tag color="blue">摘要关联 {flowQuery.data.coverage.correlated}</Tag>
                  <Tag color="orange">缺口 {flowQuery.data.coverage.missing}</Tag>
                </Space>
              </div>
              <div className="bot-flow-layer-rail">
                {flowQuery.data.layers.map((layer, index) => {
                  const evidence = EVIDENCE_META[layer.coverage];
                  return (
                    <div className="bot-flow-layer-wrap" key={layer.id}>
                      <div className={`bot-flow-layer bot-flow-layer-${layer.status}`} title={evidence.note}>
                        <span>{String(index + 1).padStart(2, "0")}</span>
                        <strong>{layer.label}</strong>
                        <Tag size="small" color={evidence.color}>{evidence.label}</Tag>
                        <small>{layer.transition_count} 条关联事件</small>
                      </div>
                      {index < flowQuery.data.layers.length - 1 && <span className="bot-flow-layer-arrow">→</span>}
                    </div>
                  );
                })}
              </div>
            </section>

            <Alert
              className="bot-flow-delivery-claim"
              type={flowQuery.data.delivery_claim.boundary === "unverified" ? "warning" : "info"}
              showIcon
              content={`交付边界：${flowQuery.data.delivery_claim.message}`}
            />

            {flowQuery.data.omissions.length > 0 && (
              <details className="bot-flow-omissions">
                <summary>证据缺口与不透明边界（{flowQuery.data.omissions.length}）</summary>
                <ul>
                  {flowQuery.data.omissions.map((omission) => (
                    <li key={`${omission.layer}:${omission.code}`}>
                      <strong>{omission.layer}</strong> · {omission.message}
                    </li>
                  ))}
                </ul>
              </details>
            )}

            <section className="bot-flow-section">
              <div className="bot-flow-section-title">
                <span>信息流转时间线</span>
                <Text type="secondary">隐藏 chain-of-thought 不采集、不重建</Text>
              </div>
              <div className="bot-flow-timeline">
                {rows.length === 0 ? (
                  <Empty description="当前事件窗口没有可投影的链路证据" />
                ) : rows.map((row) => row.type === "single" ? (
                  <FlowTransitionCard key={row.transition.id} transition={row.transition} />
                ) : (
                  <details className="bot-flow-capability-bundle" key={row.id}>
                    <summary>
                      <span className="bot-flow-dot" />
                      <strong>能力活动 · {row.transitions.length} 条</strong>
                      <Text type="secondary">工具、子 Agent、流程或 Provider 活动</Text>
                    </summary>
                    <div className="bot-flow-capability-items">
                      {row.transitions.map((transition) => (
                        <FlowTransitionCard key={transition.id} transition={transition} compact />
                      ))}
                    </div>
                  </details>
                ))}
              </div>
            </section>

          </>
        ) : null}

        {selectedTask && (
          <TaskEvidencePanel
            bot={bot}
            taskId={selectedTask.task_id}
            visible={visible}
          />
        )}
      </main>
    </div>
  );
}

function FlowTransitionCard({ transition, compact = false }: { transition: TaskFlowTransition; compact?: boolean }) {
  const evidence = EVIDENCE_META[transition.evidence_level];
  const decisionEntries = Object.entries(transition.decision).filter(([, value]) => value !== "" && value != null);
  const payloadEntries = Object.entries(transition.payload).filter(([, value]) => value !== "" && value != null);
  return (
    <details className={`bot-flow-transition bot-flow-transition-${transition.status}${compact ? " is-compact" : ""}`}>
      <summary>
        <span className="bot-flow-dot" />
        <span className="bot-flow-transition-main">
          <span className="bot-flow-transition-title">
            <strong>{transition.title}</strong>
            <Tag size="small" color={evidence.color}>{evidence.label}</Tag>
            <Tag size="small" color={jobStatusColor(transition.status)}>{taskStatusLabel(transition.status)}</Tag>
          </span>
          <span className="bot-flow-transition-route">
            {transition.source_layer} → {transition.target_layer} · {transition.kind}
          </span>
          {transition.summary && <span className="bot-flow-transition-summary">{transition.summary}</span>}
        </span>
        <span className="bot-flow-transition-time">
          <span>{fmtTime(transition.occurred_at)}</span>
          <strong>{transition.duration_ms == null ? "—" : `${Math.round(transition.duration_ms)}ms`}</strong>
        </span>
      </summary>
      <div className="bot-flow-transition-evidence">
        <div>
          <span>证据语义</span>
          <p>{evidence.note}</p>
        </div>
        {decisionEntries.length > 0 && (
          <div>
            <span>后端决定</span>
            <pre>{JSON.stringify(Object.fromEntries(decisionEntries), null, 2)}</pre>
          </div>
        )}
        {payloadEntries.length > 0 && (
          <div>
            <span>结构化元数据</span>
            <pre>{JSON.stringify(Object.fromEntries(payloadEntries), null, 2)}</pre>
          </div>
        )}
        <div>
          <span>原始证据引用</span>
          <pre>{JSON.stringify(transition.evidence, null, 2)}</pre>
        </div>
      </div>
    </details>
  );
}
