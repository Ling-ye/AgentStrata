import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Message,
  Modal,
  Skeleton,
  Space,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { api } from "../../api";
import { estimateTaskCost, formatRmb } from "../../billing";
import type {
  BotInstance,
  BotTask,
  BotTaskDetail,
  TaskRawEvent,
  TaskStepV2,
  TokenUsageV2,
} from "../../types";
import { usePolling } from "../../shared/hooks/usePolling";
import { fmtClock, fmtElapsed, fmtInt, fmtTime, jobStatusColor } from "./jobsFormat";

const { Text, Title } = Typography;

interface Props {
  visible: boolean;
  bot: BotInstance | null;
  jobs: BotTask[];
  updatedAt: number | null;
  loading: boolean;
  error: string | null;
  workspaceRoot: string;
  workspaceExists: boolean | null;
  onRefresh: (bot: BotInstance, opts?: { clear?: boolean }) => void;
  onClose: () => void;
}

type TaskGroup = { key: string; label: string; tasks: BotTask[] };

function usageTotal(usage?: TokenUsageV2 | null) {
  return Number(usage?.total_tokens || 0);
}

function usageSummary(usage?: TokenUsageV2 | null) {
  if (!usage || usageTotal(usage) <= 0) return "—";
  const cached = Number(usage.cached_tokens || usage.cache_read_tokens || 0);
  return `${fmtInt(usageTotal(usage))} Token · Cache ${fmtInt(cached)}`;
}

function liveElapsed(task: BotTask | BotTaskDetail, now: number) {
  if (
    !task.finished_at &&
    typeof task.started_at === "number" &&
    ["running", "delegated", "queued"].includes(task.status)
  ) {
    return Math.max(0, now / 1000 - task.started_at);
  }
  return task.elapsed_s ?? (
    typeof task.started_at === "number" && typeof task.finished_at === "number"
      ? task.finished_at - task.started_at
      : null
  );
}

function stepElapsed(step: TaskStepV2, now: number) {
  if (step.status === "running" && typeof step.started_at === "number") {
    return Math.max(0, now / 1000 - step.started_at);
  }
  return step.elapsed_s ?? null;
}

function formatUsageTransition(step: TaskStepV2) {
  const predicted = usageTotal(step.estimated_usage);
  const actual = usageTotal(step.actual_usage);
  if (!predicted && !actual) return "无 Token 数据";
  return `${predicted ? fmtInt(predicted) : "—"} → ${actual ? fmtInt(actual) : "进行中"}`;
}

function copyTextFallback(text: string) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("copy rejected");
}

async function copyText(text: string, label: string) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      copyTextFallback(text);
    }
    Message.success(`${label}已复制`);
  } catch {
    Message.error(`${label}复制失败`);
  }
}

function eventMatchesStep(event: TaskRawEvent, step: TaskStepV2) {
  const data = event.data || {};
  const spanId = String(data.span_id || data.step_id || "");
  const jobId = String(event.job_id || data.job_id || "");
  const stepJobId = String(step.metadata?.job_id || "");
  if (spanId) return spanId === step.step_id;
  if (jobId || stepJobId) return !!stepJobId && jobId === stepJobId;
  return (step.raw_event_types || []).includes(event.event);
}

export default function JobsModal({
  visible,
  bot,
  jobs,
  updatedAt,
  loading,
  error,
  workspaceRoot,
  workspaceExists,
  onRefresh,
  onClose,
}: Props) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<BotTaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [events, setEvents] = useState<TaskRawEvent[] | null>(null);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [mobileDetail, setMobileDetail] = useState(false);
  const [now, setNow] = useState(Date.now());

  const loadDetail = useCallback(async (silent = false) => {
    if (!bot || !selectedId) return;
    if (!silent) setDetailLoading(true);
    try {
      const next = await api.taskDetail(bot.instance_id, selectedId);
      setDetail(next);
      setDetailError("");
    } catch (reason) {
      setDetailError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (!silent) setDetailLoading(false);
    }
  }, [bot, selectedId]);

  useEffect(() => {
    if (!visible) return;
    setQuery("");
    setSelectedId("");
    setDetail(null);
    setEvents(null);
    setMobileDetail(false);
  }, [visible, bot?.instance_id]);

  useEffect(() => {
    if (!visible || jobs.length === 0) return;
    if (!selectedId || !jobs.some((task) => task.task_id === selectedId)) {
      setSelectedId(jobs[0].task_id);
    }
  }, [jobs, selectedId, visible]);

  useEffect(() => {
    setEvents(null);
    setDetail(null);
    if (visible && selectedId) void loadDetail();
  }, [loadDetail, selectedId, visible]);

  useEffect(() => {
    if (!visible) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [visible]);

  usePolling(
    visible && !!bot,
    () => {
      if (bot) onRefresh(bot, { clear: false });
      if (selectedId) void loadDetail(true);
    },
    3000,
  );

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return jobs;
    return jobs.filter((task) =>
      [
        task.task_id,
        task.description,
        task.progress,
        task.current_step,
        task.submitter,
        task.primary_model,
        ...(task.job_ids || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase()
        .includes(needle),
    );
  }, [jobs, query]);

  const groups = useMemo<TaskGroup[]>(() => {
    const running = filtered.filter((task) =>
      ["running", "delegated", "queued"].includes(task.status),
    );
    const attention = filtered.filter((task) =>
      ["failed", "error", "cancelled", "cancel_requested"].includes(task.status),
    );
    const complete = filtered.filter(
      (task) => !running.includes(task) && !attention.includes(task),
    );
    return [
      { key: "running", label: "运行中", tasks: running },
      { key: "attention", label: "需要关注", tasks: attention },
      { key: "complete", label: "最近完成", tasks: complete },
    ];
  }, [filtered]);

  const ensureEvents = useCallback(async () => {
    if (!bot || !selectedId || events !== null || eventsLoading) return;
    setEventsLoading(true);
    try {
      const response = await api.taskEvents(bot.instance_id, selectedId);
      setEvents(response.events);
    } catch (reason) {
      Message.error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setEventsLoading(false);
    }
  }, [bot, events, eventsLoading, selectedId]);

  const cost = detail ? estimateTaskCost(detail) : null;
  const baseline = detail?.forecast?.baseline;

  return (
    <Modal
      title={
        <div className="task-workbench-title">
          <span>{bot ? `${bot.display_name} · 任务可观测工作台` : "任务可观测工作台"}</span>
          <Text type="secondary">
            {updatedAt ? `3 秒自动刷新 · ${fmtClock(updatedAt)}` : "加载中…"}
          </Text>
        </div>
      }
      visible={visible}
      footer={null}
      className="task-workbench-modal"
      style={{ width: "95vw" }}
      onCancel={onClose}
    >
      <div className={`task-workbench ${mobileDetail ? "is-mobile-detail" : ""}`}>
        <aside className="task-workbench-nav">
          <div className="task-workbench-nav-tools">
            <Input.Search
              allowClear
              value={query}
              placeholder="搜索标题、ID、步骤、模型"
              onChange={setQuery}
            />
            <Button
              loading={loading}
              onClick={() => {
                if (bot) onRefresh(bot, { clear: false });
              }}
            >
              刷新
            </Button>
          </div>
          {error && <Alert type="error" content={error} showIcon />}
          {workspaceExists === false && (
            <Alert type="warning" content={`工作区不存在：${workspaceRoot || "未配置"}`} showIcon />
          )}
          {!loading && filtered.length === 0 ? (
            <Empty description={query ? "没有匹配的 v2 任务" : "暂无 schema v2 任务"} />
          ) : (
            <div className="task-nav-scroll">
              {groups.map((group) => (
                group.tasks.length > 0 && (
                  <section className="task-nav-group" key={group.key}>
                    <div className="task-nav-group-title">
                      {group.label}<span>{group.tasks.length}</span>
                    </div>
                    {group.tasks.map((task) => (
                      <button
                        type="button"
                        key={task.task_id}
                        className={`task-nav-item ${selectedId === task.task_id ? "is-selected" : ""}`}
                        onClick={() => {
                          setSelectedId(task.task_id);
                          setMobileDetail(true);
                        }}
                      >
                        <div className="task-nav-item-head">
                          <strong>{task.description || "未命名任务"}</strong>
                          <Tag size="small" color={jobStatusColor(task.status)} title={task.status}>{task.status}</Tag>
                        </div>
                        <div className="task-nav-step">{task.current_step || task.progress || "等待步骤"}</div>
                        <div className="task-nav-meta">
                          <span>{fmtElapsed(liveElapsed(task, now))}</span>
                          <span>{usageSummary(task.usage_totals)}</span>
                        </div>
                      </button>
                    ))}
                  </section>
                )
              ))}
            </div>
          )}
        </aside>

        <main className="task-workbench-detail">
          <Button className="task-mobile-back" onClick={() => setMobileDetail(false)}>
            ← 返回任务列表
          </Button>
          {detailLoading && !detail ? (
            <Skeleton text={{ rows: 8 }} animation />
          ) : detailError ? (
            <Alert type="error" content={`任务详情加载失败：${detailError}`} showIcon />
          ) : !detail ? (
            <Empty description="选择一个任务查看执行流程" />
          ) : (
            <>
              <header className="task-detail-header">
                <div className="task-detail-heading">
                  <div>
                    <Space wrap>
                      <Tag color={jobStatusColor(detail.status)} title={detail.status}>{detail.status}</Tag>
                      <Text type="secondary">{detail.primary_model || "模型待定"}</Text>
                    </Space>
                    <Title heading={5}>{detail.description || detail.task_id}</Title>
                    <Text type="secondary">{detail.current_step || detail.progress}</Text>
                  </div>
                  <Space wrap>
                    <Button size="small" onClick={() => void copyText(detail.task_id, "任务 ID")}>
                      复制任务 ID
                    </Button>
                    {(detail.job_ids || []).length > 0 && (
                      <Button size="small" onClick={() => void copyText(detail.job_ids.join("\n"), "Job ID")}>
                        复制 Job ID
                      </Button>
                    )}
                    <Button
                      size="small"
                      onClick={() => void copyText(
                        `.\\deploy\\wsl\\win\\diagnose-task.ps1 -Id ${detail.task_id}`,
                        "诊断命令",
                      )}
                    >
                      复制诊断命令
                    </Button>
                  </Space>
                </div>
                <div className="task-metric-grid">
                  <div><span>墙钟耗时</span><strong>{fmtElapsed(liveElapsed(detail, now))}</strong></div>
                  <div><span>模型 / 工具</span><strong>{fmtElapsed(detail.timing.model_s)} / {fmtElapsed(detail.timing.tool_s)}</strong></div>
                  <div><span>后台 / 路由</span><strong>{fmtElapsed(detail.timing.background_s)} / {fmtElapsed(detail.timing.routing_s)}</strong></div>
                  <div>
                    <span>固定基线</span>
                    <strong>{detail.forecast?.status === "ready" ? usageSummary(baseline) : "样本不足"}</strong>
                  </div>
                  <div><span>实际累计</span><strong>{usageSummary(detail.actual_usage)}</strong></div>
                  <div>
                    <span>实际费用估算</span>
                    <strong>{
                      detail.actual_cost?.status === "estimated"
                        ? formatRmb(detail.actual_cost.estimated_rmb)
                        : cost?.status === "estimated"
                          ? formatRmb(cost.estimatedRmb)
                          : detail.actual_cost?.status === "partial"
                            ? `${formatRmb(detail.actual_cost.estimated_rmb)}+`
                            : cost?.status === "unpriced" ? "未配置价格" : "—"
                    }</strong>
                  </div>
                </div>
              </header>

              <section className="task-timeline">
                <div className="task-section-title">
                  <span>执行时间线</span>
                  <Text type="secondary">{detail.steps.length} 个步骤 · 开始于 {fmtTime(detail.started_at ?? null)}</Text>
                </div>
                {detail.steps.length === 0 ? (
                  <Empty description="任务尚未产生步骤" />
                ) : detail.steps.map((step) => {
                  const matchingEvents = (events || []).filter((event) => eventMatchesStep(event, step));
                  return (
                    <details
                      className={`task-step task-step-${step.status}`}
                      key={step.step_id}
                      style={{ marginLeft: `${Math.min(step.depth || 0, 8) * 22}px` }}
                      onToggle={(event) => {
                        if (event.currentTarget.open) void ensureEvents();
                      }}
                    >
                      <summary>
                        <span className="task-step-dot" />
                        <span className="task-step-main">
                          <span className="task-step-title">
                            <strong>{step.title}</strong>
                            <Tag size="small" color={jobStatusColor(step.status)} title={step.status}>{step.status}</Tag>
                            <Text type="secondary">{step.type}</Text>
                          </span>
                          <span className="task-step-summary">{step.summary || step.error || "执行中…"}</span>
                        </span>
                        <span className="task-step-facts">
                          <span>{fmtTime(step.started_at ?? null)}</span>
                          <strong>{fmtElapsed(stepElapsed(step, now))}</strong>
                          <span>Token {formatUsageTransition(step)}</span>
                        </span>
                      </summary>
                      <div className="task-step-raw">
                        {eventsLoading && events === null ? (
                          <Skeleton text={{ rows: 3 }} animation />
                        ) : (
                          <pre>{JSON.stringify(
                            {
                              step,
                              events: matchingEvents,
                              note: matchingEvents.length === 0 ? "没有匹配的原始事件。" : undefined,
                            },
                            null,
                            2,
                          )}</pre>
                        )}
                      </div>
                    </details>
                  );
                })}
              </section>
            </>
          )}
        </main>
      </div>
    </Modal>
  );
}
