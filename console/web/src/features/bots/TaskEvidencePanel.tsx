import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Message,
  Skeleton,
  Space,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { api } from "../../api";
import { estimateTaskCost, formatRmb } from "../../billing";
import type {
  BotInstance,
  BotTaskDetail,
  ContextSnapshot,
  ContextSnapshotSummary,
  TaskRawEvent,
  TaskStepV2,
  TokenUsageV2,
} from "../../types";
import { usePolling } from "../../shared/hooks/usePolling";
import { fmtElapsed, fmtInt, fmtTime, jobStatusColor } from "./jobsFormat";

const { Text } = Typography;

interface Props {
  visible: boolean;
  bot: BotInstance;
  taskId: string;
}

type ContextLoadState = {
  loading: boolean;
  data?: ContextSnapshot;
  error?: string;
};

function usageTotal(usage?: TokenUsageV2 | null) {
  return Number(usage?.total_tokens || 0);
}

function usageSummary(usage?: TokenUsageV2 | null) {
  if (!usage || usageTotal(usage) <= 0) return "—";
  const cached = Number(usage.cached_tokens || usage.cache_read_tokens || 0);
  return `${fmtInt(usageTotal(usage))} Token · Cache ${fmtInt(cached)}`;
}

function liveElapsed(task: BotTaskDetail, now: number) {
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

function contextCoverage(snapshot: ContextSnapshotSummary) {
  if (snapshot.coverage === "exact_model_input") {
    return {
      label: "精确模型输入",
      color: "green",
      note: "该快照记录 AgentStrata 在本次模型调用边界实际提交的输入。",
    };
  }
  if (snapshot.coverage === "adapter_visible") {
    return {
      label: "仅适配器可见",
      color: "orange",
      note: "该快照只覆盖 AgentStrata 适配器提交的输入；Provider 管理的恢复会话和内部上下文不可见。",
    };
  }
  if (snapshot.coverage === "partial") {
    return {
      label: "部分捕获",
      color: "orange",
      note: "文本与工具上下文已记录，但明确列出的二进制载荷或受限字段只保留安全回执。",
    };
  }
  return {
    label: "Provider 不透明",
    color: "gray",
    note: "Provider 未公开本次调用的完整有效上下文，界面只展示已确认可见的部分。",
  };
}

function isPrivateReasoningKey(key: string) {
  const normalized = key.toLocaleLowerCase().replace(/[^a-z]/g, "");
  return ["analysis", "reasoning", "reasoningcontent", "thinking", "thoughts", "chainofthought", "cot"]
    .includes(normalized);
}

function visibleMessageContext(value: unknown): unknown {
  if (!Array.isArray(value)) return value;
  return value.map((rawMessage) => {
    if (!rawMessage || typeof rawMessage !== "object") return rawMessage;
    const message = rawMessage as Record<string, unknown>;
    const role = String(message.role || "").toLocaleLowerCase();
    if (role !== "assistant" && role !== "model") return message;
    return Object.fromEntries(
      Object.entries(message).filter(([key]) => !isPrivateReasoningKey(key)),
    );
  });
}

function ContextPayload({
  title,
  value,
  empty,
  assistantMessages = false,
}: {
  title: string;
  value: unknown;
  empty: string;
  assistantMessages?: boolean;
}) {
  const visibleValue = assistantMessages ? visibleMessageContext(value) : value;
  const hasValue = Array.isArray(visibleValue)
    ? visibleValue.length > 0
    : !!visibleValue && typeof visibleValue === "object" && Object.keys(visibleValue as object).length > 0;
  return (
    <div className="context-payload">
      <div className="context-payload-title">{title}</div>
      {hasValue ? (
        <pre>{JSON.stringify(visibleValue, null, 2)}</pre>
      ) : (
        <Text type="secondary">{empty}</Text>
      )}
    </div>
  );
}

export default function TaskEvidencePanel({
  visible,
  bot,
  taskId,
}: Props) {
  const selectedId = taskId;
  const [detail, setDetail] = useState<BotTaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [events, setEvents] = useState<TaskRawEvent[] | null>(null);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsTruncated, setEventsTruncated] = useState(false);
  const [eventsIntegrityGap, setEventsIntegrityGap] = useState(false);
  const [expandedStepIds, setExpandedStepIds] = useState<Set<string>>(new Set());
  const [contextLoads, setContextLoads] = useState<Record<string, ContextLoadState>>({});
  const [now, setNow] = useState(Date.now());
  const selectionRef = useRef("");
  const detailRequestRef = useRef(0);
  const detailInFlightRef = useRef<{
    key: string;
    promise: Promise<BotTaskDetail | null>;
  } | null>(null);
  const eventsRequestRef = useRef(0);
  const eventsInFlightRef = useRef<{ key: string; promise: Promise<void> } | null>(null);
  const terminalEventsRefreshRef = useRef(false);
  const requestedContextsRef = useRef(new Set<string>());
  const loadedContextsRef = useRef(new Set<string>());

  const loadDetail = useCallback((silent = false): Promise<BotTaskDetail | null> => {
    if (!bot || !selectedId) return Promise.resolve(null);
    const requestKey = `${bot.instance_id}:${selectedId}`;
    if (detailInFlightRef.current?.key === requestKey) {
      return detailInFlightRef.current.promise;
    }
    const requestId = ++detailRequestRef.current;
    const request = (async () => {
      if (!silent) setDetailLoading(true);
      try {
        const next = await api.taskDetail(bot.instance_id, selectedId);
        if (
          selectionRef.current !== requestKey
          || detailRequestRef.current !== requestId
        ) return null;
        setDetail(next);
        setDetailError("");
        return next;
      } catch (reason) {
        if (
          selectionRef.current === requestKey
          && detailRequestRef.current === requestId
        ) setDetailError(reason instanceof Error ? reason.message : String(reason));
        return null;
      } finally {
        if (detailRequestRef.current === requestId) detailInFlightRef.current = null;
        if (
          !silent
          && selectionRef.current === requestKey
          && detailRequestRef.current === requestId
        ) setDetailLoading(false);
      }
    })();
    detailInFlightRef.current = { key: requestKey, promise: request };
    return request;
  }, [bot, selectedId]);

  useEffect(() => {
    if (!visible) return;
    setDetail(null);
    setEvents(null);
    setEventsTruncated(false);
    setEventsIntegrityGap(false);
    setExpandedStepIds(new Set());
    setContextLoads({});
    requestedContextsRef.current.clear();
    loadedContextsRef.current.clear();
  }, [visible, bot.instance_id]);

  useEffect(() => {
    selectionRef.current = bot && selectedId ? `${bot.instance_id}:${selectedId}` : "";
    detailRequestRef.current += 1;
    detailInFlightRef.current = null;
    eventsRequestRef.current += 1;
    eventsInFlightRef.current = null;
    terminalEventsRefreshRef.current = false;
    setEvents(null);
    setEventsLoading(false);
    setEventsTruncated(false);
    setEventsIntegrityGap(false);
    setExpandedStepIds(new Set());
    setDetail(null);
    setDetailError("");
    setContextLoads({});
    requestedContextsRef.current.clear();
    loadedContextsRef.current.clear();
    if (visible && selectedId) void loadDetail();
  }, [bot, loadDetail, selectedId, visible]);

  useEffect(() => {
    if (!visible) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [visible]);

  const ensureEvents = useCallback((refresh = false): Promise<void> => {
    if (!bot || !selectedId || (!refresh && events !== null)) return Promise.resolve();
    const requestKey = `${bot.instance_id}:${selectedId}`;
    if (eventsInFlightRef.current?.key === requestKey) {
      return eventsInFlightRef.current.promise;
    }
    const requestId = ++eventsRequestRef.current;
    const request = (async () => {
      setEventsLoading(true);
      try {
        const response = await api.taskEvents(bot.instance_id, selectedId);
        if (
          selectionRef.current !== requestKey
          || eventsRequestRef.current !== requestId
        ) return;
        setEvents(response.events);
        setEventsTruncated(response.truncated);
        setEventsIntegrityGap(response.integrity_gap);
      } catch (reason) {
        if (
          selectionRef.current === requestKey
          && eventsRequestRef.current === requestId
        ) Message.error(reason instanceof Error ? reason.message : String(reason));
      } finally {
        if (eventsRequestRef.current === requestId) eventsInFlightRef.current = null;
        if (
          selectionRef.current === requestKey
          && eventsRequestRef.current === requestId
        ) setEventsLoading(false);
      }
    })();
    eventsInFlightRef.current = { key: requestKey, promise: request };
    return request;
  }, [bot, events, selectedId]);

  const ensureContext = useCallback(async (snapshotId: string) => {
    if (!bot || !selectedId) return;
    const requestKey = `${bot.instance_id}:${selectedId}`;
    const cacheKey = `${requestKey}:${snapshotId}`;
    if (
      requestedContextsRef.current.has(cacheKey)
      || loadedContextsRef.current.has(cacheKey)
    ) return;
    requestedContextsRef.current.add(cacheKey);
    setContextLoads((current) => ({
      ...current,
      [snapshotId]: { loading: true },
    }));
    try {
      const data = await api.taskContext(bot.instance_id, selectedId, snapshotId);
      if (selectionRef.current !== requestKey) return;
      loadedContextsRef.current.add(cacheKey);
      setContextLoads((current) => ({
        ...current,
        [snapshotId]: { loading: false, data },
      }));
    } catch (reason) {
      if (selectionRef.current !== requestKey) return;
      setContextLoads((current) => ({
        ...current,
        [snapshotId]: {
          loading: false,
          error: reason instanceof Error ? reason.message : String(reason),
        },
      }));
    } finally {
      requestedContextsRef.current.delete(cacheKey);
    }
  }, [bot, selectedId]);

  usePolling(
    visible && !!selectedId,
    async () => {
      const wasActive = !!detail
        && ["running", "delegated", "queued"].includes(detail.status);
      const nextDetail = selectedId ? await loadDetail(true) : null;
      const isActive = !!nextDetail
        && ["running", "delegated", "queued"].includes(nextDetail.status);
      const transitionedToTerminal = wasActive && !!nextDetail && !isActive;
      const pendingTerminalRefresh = terminalEventsRefreshRef.current;
      if (transitionedToTerminal) terminalEventsRefreshRef.current = true;
      if (
        events !== null
        && expandedStepIds.size > 0
        && (isActive || transitionedToTerminal || pendingTerminalRefresh)
      ) {
        await ensureEvents(true);
        if (pendingTerminalRefresh && !transitionedToTerminal) {
          terminalEventsRefreshRef.current = false;
        }
      }
    },
    3000,
  );

  const cost = detail ? estimateTaskCost(detail) : null;
  const baseline = detail?.forecast?.baseline;
  const retainedContextCount = detail?.summary_limits?.context_snapshots_retained
    ?? detail?.context_snapshots?.length
    ?? 0;
  const totalContextCount = Math.max(
    retainedContextCount,
    detail?.summary_limits?.context_snapshots_total ?? retainedContextCount,
  );
  const contextSummaryLimited = !!detail?.summary_limits
    && (
      detail.summary_limits.context_snapshots_truncated
      || detail.summary_limits.context_snapshots_minimal
    );
  const timelineSummaryLimited = !!detail?.summary_limits
    && (
      detail.summary_limits.tools_retained < detail.summary_limits.tools_total
      || detail.summary_limits.steps_retained < detail.summary_limits.steps_total
    );

  if (!visible || !selectedId) return null;

  return (
    <section className="task-evidence-inline" aria-label="完整任务信息">
      <div className="task-workbench-detail">
          {detailLoading && !detail ? (
            <Skeleton text={{ rows: 8 }} animation />
          ) : detailError ? (
            <Alert type="error" content={`任务详情加载失败：${detailError}`} showIcon />
          ) : !detail ? (
            <Empty description="选择一个任务查看执行流程" />
          ) : (
            <>
              <section className="task-detail-header">
                <div className="task-section-title">
                  <span>完整任务信息</span>
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
                  <div title="模型与后端活动 span 可能并行或嵌套，不能相加为墙钟耗时。">
                    <span>模型跨度 / 活动跨度*</span>
                    <strong>{fmtElapsed(detail.timing.model_s)} / {fmtElapsed(detail.timing.activity_s)}</strong>
                  </div>
                  <div><span>工具 / 后台 / 路由</span><strong>{fmtElapsed(detail.timing.tool_s)} / {fmtElapsed(detail.timing.background_s)} / {fmtElapsed(detail.timing.routing_s)}</strong></div>
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
              </section>

              <section className="task-context-section">
                <div className="task-section-title">
                  <span>上下文快照</span>
                  <Text type="secondary">
                    {retainedContextCount === totalContextCount
                      ? `${retainedContextCount} 次模型调用边界`
                      : `保留 ${retainedContextCount}/${totalContextCount} 次模型调用边界`}
                    {" · Provider 私有推理未采集、未展示"}
                  </Text>
                </div>
                {contextSummaryLimited && (
                  <Alert
                    type="warning"
                    content={[
                      retainedContextCount < totalContextCount
                        ? `上下文快照摘要仅保留 ${retainedContextCount}/${totalContextCount}；其余调用无法从当前任务摘要授权加载。`
                        : "",
                      detail.summary_limits?.context_snapshots_minimal
                        ? "为维持 task.json 总量上限，当前仅保留上下文 artifact 的索引元数据；展开卡片仍会按 snapshot ID 懒加载已持久化正文。"
                        : "",
                    ].filter(Boolean).join(" ")}
                    showIcon
                  />
                )}
                {(detail.context_snapshots || []).length === 0 ? (
                  <Empty description={totalContextCount > 0
                    ? "该任务存在上下文边界，但快照索引未保留在当前有界摘要中。"
                    : "该任务没有可用的上下文快照（旧任务或尚未到达模型调用边界）"}
                  />
                ) : (
                  <div className="context-snapshot-list">
                    {(detail.context_snapshots || []).map((snapshot) => {
                      const coverage = contextCoverage(snapshot);
                      const load = contextLoads[snapshot.snapshot_id];
                      const parentStep = detail.steps.find(
                        (step) => step.step_id === snapshot.parent_span_id,
                      );
                      const contextOwner = snapshot.role === "subagent" || snapshot.depth > 0
                        ? `subagent${parentStep?.title ? ` · ${parentStep.title}` : ""}`
                        : "main";
                      const effectiveTitle = snapshot.coverage === "exact_model_input"
                        ? "实际模型输入"
                        : snapshot.coverage === "adapter_visible"
                          ? "实际模型输入（仅适配器可见部分）"
                          : snapshot.coverage === "partial"
                            ? "可见模型输入（受限字段与二进制以回执代替）"
                            : "实际模型输入（Provider 未公开）";
                      return (
                        <details
                          className="context-snapshot"
                          key={snapshot.snapshot_id}
                          onToggle={(event) => {
                            if (
                              event.currentTarget.open
                              && snapshot.capture_status !== "unavailable"
                            ) {
                              void ensureContext(snapshot.snapshot_id);
                            }
                          }}
                        >
                          <summary>
                            <span className="context-snapshot-main">
                              <span className="context-snapshot-title">
                                <strong>{snapshot.model || "未知模型"}</strong>
                                <Tag size="small" color={coverage.color}>{coverage.label}</Tag>
                                {snapshot.capture_status === "unavailable" ? (
                                  <Tag size="small" color="red">正文未持久化</Tag>
                                ) : (
                                  <Tag size="small" color="blue">
                                    {snapshot.redacted ? "含脱敏替换" : "持久化前已检查"}
                                  </Tag>
                                )}
                                {snapshot.truncated && <Tag size="small" color="orange">已截断</Tag>}
                                {snapshot.capture_status !== "captured" && (
                                  <Tag size="small" color="red">{snapshot.capture_status}</Tag>
                                )}
                              </span>
                              <span className="context-snapshot-subtitle">
                                {snapshot.backend || "未知 backend"} · {contextOwner} · 第 {snapshot.iteration + 1} 次调用
                                {snapshot.reasoning_effort ? ` · ${snapshot.reasoning_effort}` : ""}
                              </span>
                            </span>
                            <span className="context-snapshot-facts">
                              <span>{fmtTime(snapshot.captured_at)}</span>
                              <strong>{fmtInt(snapshot.estimated_tokens)} Token（估算）</strong>
                              <span>
                                会话 {snapshot.message_count} · 输入 {snapshot.effective_message_count} · 工具 {snapshot.tool_schema_count} · 资源 {snapshot.resource_count}
                              </span>
                            </span>
                          </summary>
                          <div className="context-snapshot-body">
                            <Alert
                              type={snapshot.coverage === "exact_model_input" ? "info" : "warning"}
                              content={[
                                coverage.note,
                                snapshot.redacted ? "展示内容包含持久化前生成的脱敏占位符。" : "内容已通过持久化前脱敏检查。",
                                snapshot.truncated ? "该 artifact 超过大小上限，正文已替换为摘要清单，不能视为完整正文。" : "",
                                "Provider 私有 hidden reasoning 不属于可监控上下文。",
                              ].filter(Boolean).join(" ")}
                              showIcon
                            />
                            {snapshot.omitted.length > 0 && (
                              <div className="context-omitted">
                                <strong>未覆盖：</strong>{snapshot.omitted.join("、")}
                              </div>
                            )}
                            {snapshot.capture_status === "unavailable" ? (
                              <Alert
                                type="error"
                                content="模型调用边界已记录，但上下文正文未能安全持久化；这不是旧任务，也不能解释为零上下文。"
                                showIcon
                              />
                            ) : load?.loading ? (
                              <Skeleton text={{ rows: 6 }} animation />
                            ) : load?.error ? (
                              <Alert
                                type="error"
                                content={`上下文快照加载失败：${load.error}`}
                                showIcon
                              />
                            ) : load?.data ? (
                              <>
                                <div className="context-payload-grid">
                                  <ContextPayload
                                    title="AgentStrata 会话历史"
                                    value={load.data.session_messages}
                                    empty="没有记录 AgentStrata 可见的会话历史。"
                                    assistantMessages
                                  />
                                  <ContextPayload
                                    title={effectiveTitle}
                                    value={load.data.effective_messages}
                                    empty={snapshot.coverage === "provider_opaque"
                                      ? "Provider 未公开实际模型输入。"
                                      : "没有记录可见的模型输入。"}
                                    assistantMessages
                                  />
                                </div>
                                <div className="context-payload-grid context-payload-grid-secondary">
                                  <ContextPayload
                                    title="工具 Schema"
                                    value={load.data.tool_schemas}
                                    empty="本次调用没有提交工具 Schema。"
                                  />
                                  <ContextPayload
                                    title="资源回执"
                                    value={load.data.resources}
                                    empty="本次调用没有资源回执。"
                                  />
                                </div>
                                <ContextPayload
                                  title="模型选择与上下文元数据"
                                  value={{
                                    model_selection: load.data.model_selection,
                                    context_kind: snapshot.context_kind,
                                    coverage: snapshot.coverage,
                                    omitted: snapshot.omitted,
                                  }}
                                  empty="没有模型选择或上下文元数据。"
                                />
                                {load.data.sanitization && (
                                  <ContextPayload
                                    title="脱敏说明"
                                    value={load.data.sanitization}
                                    empty="没有脱敏说明。"
                                  />
                                )}
                              </>
                            ) : null}
                          </div>
                        </details>
                      );
                    })}
                  </div>
                )}
              </section>

              <section className="task-timeline">
                <div className="task-section-title">
                  <span>执行时间线</span>
                  <Text type="secondary">{detail.steps.length} 个步骤 · 开始于 {fmtTime(detail.started_at ?? null)}</Text>
                </div>
                {eventsTruncated && (
                  <Alert
                    type="warning"
                    content={eventsIntegrityGap
                      ? "原始事件尾部存在不安全权限、损坏/半写记录或序列缺口；仅展示已验证记录，不能视为完整时间线。"
                      : "原始事件仅展示有界尾部，较早事件未传输；结构化步骤与上下文摘要仍来自任务详情。"}
                    showIcon
                  />
                )}
                {(detail.activity_summary?.truncated || timelineSummaryLimited) && (
                  <Alert
                    type="warning"
                    content={[
                      detail.activity_summary?.truncated
                        ? `Provider activity 摘要已达上限：保留 ${detail.activity_summary.provider_retained}/${detail.activity_summary.provider_total}。`
                        : "",
                      timelineSummaryLimited
                        ? `任务摘要工具保留 ${detail.summary_limits?.tools_retained}/${detail.summary_limits?.tools_total}，步骤保留 ${detail.summary_limits?.steps_retained}/${detail.summary_limits?.steps_total}。`
                        : "",
                      "原始事件尾部仍可用于最近活动诊断。",
                    ].filter(Boolean).join(" ")}
                    showIcon
                  />
                )}
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
                        setExpandedStepIds((current) => {
                          const next = new Set(current);
                          if (event.currentTarget.open) next.add(step.step_id);
                          else next.delete(step.step_id);
                          return next;
                        });
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
      </div>
    </section>
  );
}
