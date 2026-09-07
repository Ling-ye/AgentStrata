import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Empty, Space, Tag } from "@arco-design/web-react";
import type { GatewayObservation, GatewayRunDetail } from "./model";
import { DELIVERY_STAGES, layerName, OBSERVATION_NAMES, runState } from "./model";
import { bodyState, buildRunView, dateTime, duration, related, runDuration, stepIsOpen, type DisplayStep, type ObservationBody } from "./workbenchModel";
import { ConfigFields, Disclosure, ObservationPayload, TextPreview } from "./ObservationContent";

const EVENT_LABELS: Record<string, string> = {
  actor_execution: "Agent 执行", actor_returned: "Agent 执行",
  ContextSnapshotPrepared: "准备上下文", session_capabilities: "本次可用能力",
  InputResourcesDispatched: "输入资源", TurnError: "任务异常",
};
function label(event: GatewayObservation) {
  const name = event.name || event.model || String(event.data?.name || event.data?.model || "");
  if (event.kind.startsWith("LlmCall")) return "模型调用 · " + (name || "未记录模型");
  if (event.kind.startsWith("Tool")) return "工具调用 · " + name;
  return name || EVENT_LABELS[event.kind] || OBSERVATION_NAMES[event.kind] || event.kind;
}
const METADATA = new Set(["name", "model", "trace_id", "span_id", "parent_span_id", "depth", "configuration_id", "context_snapshot_id", "snapshot_id"]);
function visibleMetadata(event?: GatewayObservation) {
  return Object.fromEntries(Object.entries(event?.data ?? {}).filter(([key, value]) => !METADATA.has(key) && value !== "" && value != null));
}

type BodyScope = { instanceId: string; runId: string; expired: boolean; active: boolean };
function EventBody({ event, title, scope }: { event: GatewayObservation; title: string; scope: BodyScope }) {
  return <ObservationPayload {...scope} reference={event.body_ref} captureState={event.body_state} title={title} />;
}
function Permissions({ events, scope }: { events: GatewayObservation[]; scope: BodyScope }) {
  return <>{events.map((event) => <div key={event.seq} className="obs-permission">
    <div className="obs-permission-summary"><Tag color={event.data?.allowed === true ? "green" : event.data?.allowed === false ? "red" : "gray"}>
      {event.data?.allowed === true ? "允许" : event.data?.allowed === false ? "拒绝" : "决定未记录"}</Tag>
      <span>{String(event.data?.name ?? "")}</span><span>{event.data?.phase === "execution" ? "执行检查" : event.data?.phase === "visibility" ? "可见性检查" : "检查阶段未记录"}</span>
      <code>{String(event.data?.code ?? "")}</code></div>
    <Disclosure title="权限记录"><EventBody event={event} title="权限详情" scope={scope} /></Disclosure>
  </div>)}</>;
}
function Logs({ events, scope }: { events: GatewayObservation[]; scope: BodyScope }) {
  return <>{events.map((event) => <Disclosure key={event.seq} title={String(event.data?.level ?? "INFO") + " · " + dateTime(event.created_at) + " · " + String(event.data?.logger ?? "")}>
    <EventBody event={event} title="日志正文" scope={scope} />
  </Disclosure>)}</>;
}

function StepCard({ step, index, open, terminal, scope, highlighted, onToggle, onConfig }: {
  step: DisplayStep; index: number; open: boolean; terminal: boolean; scope: BodyScope; highlighted: boolean;
  onToggle: (key: string, value: boolean) => void; onConfig: (event: GatewayObservation) => void;
}) {
  const event = step.event;
  const cached = useQuery<ObservationBody>({ queryKey: ["observation-body", scope.instanceId, scope.runId, event.body_ref], enabled: false });
  const body = !scope.expired && cached.data?.payload && typeof cached.data.payload === "object" ?
    cached.data.payload as Record<string, unknown> : {};
  const error = String(body.error || body.message && event.status === "failed" && body.message || event.data?.code || "");
  const summary = String(body.summary || event.data?.summary || event.data?.finish_reason ||
    (event.data?.tool_count != null ? "本次可用工具 " + event.data.tool_count + " 个" : ""));
  const incomplete = !!step.start && !step.finish && terminal;
  const state = incomplete ? { label: "结束未记录", color: "orange" } : runState(event.status ?? "unknown");
  const elapsed = event.elapsed_ms ?? (step.start && step.finish ? Math.max(0, step.finish.created_at - step.start.created_at) * 1000 :
    step.start && !terminal ? (Date.now() / 1000 - step.start.created_at) * 1000 : null);
  const usage = event.data?.usage as Record<string, number> | undefined;
  const tokens = event.total_tokens ?? usage?.total_tokens;
  const estimated = step.start?.data?.input_estimated_tokens ?? event.data?.input_estimated_tokens;
  const boundary = step.start ?? event;
  const payloadEvents = [step.start, step.finish ?? (!step.start ? event : undefined)].filter((item): item is GatewayObservation => !!item);
  return <article data-step-key={step.key} data-entity-id={event.entity_id} data-status={incomplete ? "unknown" : event.status}
    className={"obs-step-card" + (highlighted ? " is-highlighted" : "") + (event.status === "failed" ? " is-failed" : "")}
    style={{ "--step-depth": Math.min(step.depth, 6) } as CSSProperties}>
    <div className="obs-step-header">
      <button type="button" className="obs-step-toggle" aria-expanded={open} aria-controls={"obs-step-body-" + event.seq}
        onClick={() => onToggle(step.key, !open)}>
        <span className="obs-step-dot" aria-hidden />
        <span className="obs-step-main"><span className="obs-step-title"><span className="obs-step-index">{String(index + 1).padStart(2, "0")}</span><strong>{label(event)}</strong><Tag size="small" color={state.color}>{state.label}</Tag></span>
          <span className="obs-step-route">{event.source && event.target ? layerName(event.source) + " → " + layerName(event.target) : layerName(event.layer ?? event.target ?? "gateway")}</span>
          {summary && <span className="obs-step-summary">{summary}</span>}
          {error && <span className="obs-step-error">{error}</span>}
          {step.missingParent && <span className="obs-step-gap">父阶段未记录</span>}
          {step.finish && !step.start && <span className="obs-step-gap">开始未记录</span>}
        </span>
        <span className="obs-step-facts"><span>{dateTime(step.start?.created_at ?? event.created_at)}</span><strong>{duration(elapsed)}</strong>
          {tokens != null && <span>实际 {tokens.toLocaleString()} Token</span>}
          {typeof estimated === "number" && estimated > 0 && <span>输入估算 {estimated.toLocaleString()} Token</span>}</span>
        <span className="obs-step-chevron" aria-hidden>{open ? "−" : "+"}</span>
      </button>
      {boundary.entity_id && <Button size="mini" className="obs-step-config" onClick={() => onConfig(boundary)}>查看配置</Button>}
    </div>
    {step.permissions.length > 0 && <div className="obs-step-permissions"><Permissions events={step.permissions} scope={scope} /></div>}
    {open && <div className="obs-step-body" id={"obs-step-body-" + event.seq}>
      {event.data?.relation === "background_task" && <Alert type="info" content={"后台任务 " + String(event.data.related_task_id) + " · " + String(event.data.related_task_state ?? "状态未记录") + "。独立执行过程尚未接入。"} />}
      <div className="obs-step-payloads">{payloadEvents.map((item) => <section key={item.seq}>
        {item.body_ref || item.body_state && item.body_state !== "not_recorded" ?
          <EventBody event={item} scope={scope} title={item.phase === "start" ? "调用参数" : item.phase === "finish" ? "调用结果" : "阶段数据"} /> :
          <><h4>{item.phase === "start" ? "调用输入" : item.phase === "finish" ? "调用结果" : "阶段数据"}</h4>
            <ConfigFields value={visibleMetadata(item)} />
            {item.kind.startsWith("LlmCall") && <p className="obs-muted">{item.phase === "start" ? "输入正文见下方关联上下文。" : "此阶段未记录模型返回正文。"}</p>}</>}
      </section>)}</div>
      {step.contexts.map((context) => <Disclosure key={context.seq} title="上下文">
        <EventBody event={context} scope={scope} title="模型可见上下文" />
      </Disclosure>)}
      {event.kind.startsWith("LlmCall") && !step.contexts.length && <p className="obs-muted">未记录可关联的上下文快照</p>}
      {!!step.logs.length && <section aria-label="步骤日志"><h4>步骤日志</h4><Logs events={step.logs} scope={scope} /></section>}
      <Disclosure title="阶段原始记录"><TextPreview text={JSON.stringify({ start: step.start, finish: step.finish, event: !step.start && !step.finish ? event : undefined }, null, 2)} /></Disclosure>
    </div>}
  </article>;
}

function loadExpanded(key: string): Record<string, boolean> {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ?
      Object.fromEntries(Object.entries(value).filter(([, item]) => typeof item === "boolean")) as Record<string, boolean> : {};
  } catch { return {}; }
}

export default function RunInspector({ instanceId, detail, events, visible, focusRequest, onViewConfig, onMore, hasMore, fetchingMore }: {
  instanceId: string; detail: GatewayRunDetail; events: GatewayObservation[]; visible: boolean;
  focusRequest?: { entity: string; revision: number }; onViewConfig: (event: GatewayObservation) => void;
  onMore: () => void; hasMore: boolean; fetchingMore: boolean;
}) {
  const run = detail.run;
  const storageKey = "obs:steps:" + instanceId + ":" + run.run_id;
  const [expanded, setExpanded] = useState(() => loadExpanded(storageKey));
  const [, tick] = useState(0);
  const container = useRef<HTMLElement>(null);
  const view = useMemo(() => buildRunView(events), [events]);
  const terminal = ["completed", "failed", "aborted"].includes(run.state);
  const scope: BodyScope = { instanceId, runId: run.run_id, expired: !!run.details_expired, active: visible };
  useEffect(() => { try { sessionStorage.setItem(storageKey, JSON.stringify(expanded)); } catch { /* Browser storage can be disabled. */ } }, [expanded, storageKey]);
  useEffect(() => {
    if (terminal || !visible) return;
    const timer = window.setInterval(() => tick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [terminal, visible]);
  useEffect(() => {
    if (!focusRequest) return;
    const matching = view.steps.filter((step) => related(step.event, focusRequest.entity) || step.start && related(step.start, focusRequest.entity));
    setExpanded((current) => ({ ...current, ...Object.fromEntries(matching.map((step) => [step.key, true])) }));
    const target = Array.from(container.current?.querySelectorAll<HTMLElement>("[data-step-key]") ?? [])
      .find((element) => element.dataset.stepKey === matching[0]?.key);
    const frame = requestAnimationFrame(() => {
      target?.scrollIntoView({ block: "start" });
      target?.querySelector<HTMLButtonElement>(".obs-step-toggle")?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
    // A locator action scrolls once; polling must not move the reader.
  }, [focusRequest]);
  const toggle = (key: string, value: boolean) => setExpanded((current) => ({ ...current, [key]: value }));
  return <section ref={container} className="obs-run" aria-label="任务运行过程">
    <header className="obs-run-heading"><div><strong>任务运行</strong><code title={run.run_id}>{run.run_id}</code></div>
      <Tag color={runState(run.state).color}>{runState(run.state).label}</Tag></header>
    <div className="obs-run-meta"><span>开始 {dateTime(run.started_at ?? run.created_at)}</span><span>耗时 {duration(runDuration(run))}</span>
      <span>{run.backend ?? "Backend 未记录"} · {run.model ?? "模型未记录"}</span><span>角色 {run.role ?? "未记录"}</span>
      <span>配置 {(run.config_revision ?? run.config_id)?.slice(0, 10) ?? "未记录"}</span>
      <span>实际 Token {run.total_tokens?.toLocaleString() ?? "未记录"}</span></div>
    {!!run.details_expired && <Alert type="info" content="详细正文已到期；任务摘要和阶段指标仍保留。" />}
    {["truncated", "capture_failed"].includes(run.capture_state ?? "") && <Alert type="warning" content={"任务记录" + bodyState(run.capture_state!) + "，部分过程或正文不可用。"} />}
    {detail.legacy && <Alert type="info" content="历史记录未包含执行时配置及完整调用详情。" />}
    {(detail.truncated || detail.sanitization_truncated) && <Alert type="warning" content="当前响应包含截断记录，不能视为完整过程。" />}
    <ObservationPayload {...scope} reference={run.input_ref} title="任务输入" />
    <div className="obs-flow-toolbar"><strong>执行过程 · {view.steps.length} 步{hasMore ? "（还有后续记录）" : ""}</strong><Space size="small">
      <Button size="mini" onClick={() => setExpanded((current) => ({ ...current, ...Object.fromEntries(view.steps.map((step) => [step.key, true])) }))}>展开全部</Button>
      <Button size="mini" onClick={() => setExpanded((current) => ({ ...current, ...Object.fromEntries(view.steps.map((step) => [step.key, false])) }))}>收起全部</Button>
    </Space></div>
    <div className="obs-flow" aria-label="执行时间轴">{view.steps.length ? view.steps.map((step, index) =>
      <StepCard key={step.key} step={step} index={index} open={stepIsOpen(step, expanded, terminal)} terminal={terminal} scope={scope}
        highlighted={!!focusRequest && related(step.event, focusRequest.entity)} onToggle={toggle} onConfig={onViewConfig} />) :
      <Empty description="尚未记录执行阶段" />}</div>
    {hasMore && <Button long loading={fetchingMore} onClick={onMore}>加载后续记录</Button>}
    <section className="obs-run-result">
      <ObservationPayload {...scope} reference={run.result_ref} title="任务结果" />
      {!run.result_ref && !run.details_expired && run.final_text && <TextPreview text={run.final_text} />}
      {run.error_code && <p className="obs-step-error">任务错误码：{run.error_code}</p>}
      <h4>消息交付</h4>
      {detail.receipts.length ? <ol className="obs-receipts">{detail.receipts.map((receipt) => <li key={receipt.receipt_id}>
        <Tag>{DELIVERY_STAGES[receipt.stage] ?? receipt.stage}</Tag><span>{dateTime(receipt.observed_at)}</span>
        <code>{receipt.outbound_id}</code>{receipt.error_code && <span className="obs-step-error">{receipt.error_code}</span>}</li>)}</ol> :
        <p className="obs-muted">尚未记录交付回执</p>}
      {!!detail.outbox.length && <ConfigFields value={{ "出站状态": detail.outbox }} />}
      {!!view.permissions.length && <section aria-label="任务权限记录"><h4>任务权限记录</h4>
        <p className="obs-muted">以下记录未绑定到具体调用。</p><Permissions events={view.permissions} scope={scope} /></section>}
      {!!detail.approvals.length && <Disclosure title="任务审批"><ConfigFields value={detail.approvals} /></Disclosure>}
      {!!view.logs.length && <section aria-label="任务日志"><h4>任务日志</h4><Logs events={view.logs} scope={scope} /></section>}
      <Disclosure title="任务状态记录"><ConfigFields value={{ "采集状态": bodyState(run.capture_state ?? "not_recorded"),
        "结束时间": dateTime(run.finished_at), "详情到期": dateTime(run.details_expires_at) }} />
        <TextPreview text={JSON.stringify(view.states, null, 2)} /></Disclosure>
    </section>
  </section>;
}
