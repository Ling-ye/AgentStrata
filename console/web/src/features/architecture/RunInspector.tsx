import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Empty, Space, Tag } from "@arco-design/web-react";
import type { GatewayObservation, GatewayRunDetail } from "./model";
import { DELIVERY_STAGES, layerName, OBSERVATION_NAMES, runState } from "./model";
import { bodyState, buildRunView, dateTime, duration, runDuration, stepDuration, stepIsOpen, stepState, rememberOpenedSteps, RUNTIME_LAYERS, RUNTIME_OPERATIONS, type FlowItem, type ObservationBody } from "./workbenchModel";
import { ConfigFields, DetailScope, Disclosure, ObservationPayload, TaskDetailState, TextPreview } from "./ObservationContent";
import ExecutionConfiguration from "./ExecutionConfiguration";
import { agentProcess } from "./agentProcessModel";

const EVENT_LABELS: Record<string, string> = {
  ContextSnapshotPrepared: "准备上下文", session_capabilities: "本次可用能力",
  InputResourcesDispatched: "输入资源", TurnError: "任务异常",
  AgentProcessCaptureFailed: "执行过程采集失败",
  ToolCatalogObserved: "MCP 工具接入",
};
function label(event: GatewayObservation) {
  if (event.kind === "ToolCatalogObserved") return event.data?.catalog_phase === "failed" ? "MCP 工具接入失败" : event.data?.catalog_phase === "list_response_prepared" ? "已处理 MCP 工具列表请求" : "MCP 服务初始化";
  const name = event.name || event.model || String(event.data?.name || event.data?.model || "");
  if (event.kind.startsWith("LlmCall")) return "模型调用 · " + (name || "未记录模型");
  if (event.kind.startsWith("Tool")) return "工具调用 · " + name;
  return name || EVENT_LABELS[event.kind] || OBSERVATION_NAMES[event.kind] || event.kind;
}
const METADATA = new Set(["name", "model", "trace_id", "span_id", "parent_span_id", "depth", "configuration_id", "context_snapshot_id", "snapshot_id", "flow_version", "runtime_layer", "stage_span_id"]);
function visibleMetadata(event?: GatewayObservation) {
  return Object.fromEntries(Object.entries(event?.data ?? {}).filter(([key, value]) => !METADATA.has(key) && value !== "" && value != null));
}

function errorText(value: unknown) {
  if (!value) return "";
  if (typeof value === "object") {
    const error = value as Record<string, unknown>;
    return String(error.message || error.code || "错误详情见输出记录");
  }
  return String(value);
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
    <Disclosure stateKey={`permission:${event.seq}`} title="权限记录"><EventBody event={event} title="权限详情" scope={scope} /></Disclosure>
  </div>)}</>;
}
function Logs({ events, scope }: { events: GatewayObservation[]; scope: BodyScope }) {
  return <>{events.map((event) => <Disclosure key={event.seq} stateKey={`log:${event.seq}`} title={String(event.data?.level ?? "INFO") + " · " + dateTime(event.created_at) + " · " + String(event.data?.logger ?? "")}>
    <EventBody event={event} title="日志正文" scope={scope} />
  </Disclosure>)}</>;
}

function route(event: GatewayObservation) {
  const source = event.data?.source ?? event.source;
  const target = event.data?.target ?? event.target;
  const name = (value: string) => RUNTIME_LAYERS[value as keyof typeof RUNTIME_LAYERS] ?? layerName(value);
  return typeof source === "string" && typeof target === "string" ? name(source) + " → " + name(target) : "";
}

function StepCard({ item, index, open, terminal, scope, onToggle, hasMore }: {
  item: FlowItem; index: number; open: boolean; terminal: boolean; scope: BodyScope; hasMore: boolean;
  onToggle: (key: string, value: boolean) => void;
}) {
  const step = item.step;
  const event = step.event;
  const cached = useQuery<ObservationBody>({ queryKey: ["observation-body", scope.instanceId, scope.runId, event.body_ref], enabled: false });
  const body = !scope.expired && cached.data?.payload && typeof cached.data.payload === "object" ?
    cached.data.payload as Record<string, unknown> : {};
  const error = errorText(step.delivery?.errorCode || body.error || body.message && event.status === "failed" && body.message || event.data?.code);
  const rawSummary = String(body.summary || event.data?.summary || event.data?.finish_reason ||
    (event.data?.catalog_tool_count != null ? "工具列表：" + event.data.catalog_tool_count + " 项" : event.data?.tool_count != null ? "本次可用工具 " + event.data.tool_count + " 个" : ""));
  const summary = ["completed", "in_progress", "stop"].includes(rawSummary) ? "" : rawSummary;
  const state = stepState(step, terminal, hasMore);
  const elapsed = stepDuration(step, terminal);
  const usage = event.data?.usage as Record<string, number> | undefined;
  const tokens = event.total_tokens ?? usage?.total_tokens;
  const estimated = step.start?.data?.input_estimated_tokens ?? event.data?.input_estimated_tokens;
  const boundary = step.start ?? event;
  const process = agentProcess(step);
  const payloadEvents = [step.start, step.finish ?? (!step.start ? event : undefined)].filter((item): item is GatewayObservation => !!item);
  return <DetailScope id={`step:${step.key}`}><article data-step-key={step.key} data-entity-id={event.entity_id} data-status={state.status}
    data-runtime-layer={item.layer} data-stage-key={item.stageKey} data-process-kind={process.kind}
    className={"obs-step-card" + (state.status === "failed" ? " is-failed" : "")}
    style={{ "--step-depth": Math.min(step.depth, 6) } as CSSProperties}>
    <div className="obs-step-header">
      <button type="button" className="obs-step-toggle" aria-expanded={open} aria-controls={"obs-step-body-" + event.seq}
        onClick={() => onToggle(step.key, !open)}>
        <span className="obs-step-dot" aria-hidden />
        <span className="obs-step-main"><span className="obs-step-title"><span className="obs-step-index">{String(index + 1).padStart(2, "0")}</span><strong>{process.supported ? process.title : label(event)}</strong><Tag size="small" color={state.color}>{state.label}</Tag></span>
          {!process.supported && <span className="obs-step-route">{item.layer && <b>{RUNTIME_LAYERS[item.layer]} · </b>}{route(boundary) || (!item.layer ? "职责归属未记录" : "层内调用")}</span>}
          {summary && <span className="obs-step-summary">{summary}</span>}
          {process.supported && <span className="obs-step-route">{step.agentName}{process.source ? " · " + process.source : ""}{event.data?.backend ? " · " + String(event.data.backend) : ""}
            {step.modelIteration != null ? ` · 来自第 ${step.modelIteration + 1} 轮模型调用` : ""}</span>}
          {error && <span className="obs-step-error">{error}</span>}
          {step.finish && !step.start && !process.message && <span className="obs-step-gap">{hasMore ? "开始记录尚未取得" : "开始未记录"}</span>}
        </span>
        <span className="obs-step-facts"><span>{dateTime(step.start?.created_at ?? event.created_at)}</span><strong>{duration(elapsed)}</strong>
          {tokens != null && <span>实际 {tokens.toLocaleString()} Token</span>}
          {typeof estimated === "number" && estimated > 0 && <span>输入估算 {estimated.toLocaleString()} Token</span>}</span>
        <span className="obs-step-chevron" aria-hidden>{open ? "−" : "+"}</span>
      </button>
    </div>
    {process.supported && <div className="obs-step-payloads obs-process-panels">{process.panels.filter((panel) => !panel.secondary).map((panel) =>
      <DetailScope id={panel.id} key={panel.id}><ObservationPayload {...scope}
        reference={panel.event?.body_ref} captureState={panel.event?.body_state ?? (!terminal && panel.id === "output" ? "pending" : "not_recorded")}
        title={panel.title} select={panel.select} preview={!open} messages={panel.messages} contentId={panel.id} />
      </DetailScope>)}</div>}
    {step.permissions.length > 0 && <div className="obs-step-permissions"><Permissions events={step.permissions} scope={scope} /></div>}
    {open && <div className="obs-step-body" id={"obs-step-body-" + event.seq}>
      {step.missingParent && <p className="obs-muted">{hasMore ? "上级调用尚未取得，还有后续记录可加载。" : "上级调用未采集，本步骤的输入、结果和状态可独立查看。"}</p>}
      {item.missingStage && <p className="obs-muted">{hasMore ? "所属阶段尚未取得。" : "所属阶段未记录，本步骤保留原始关联。"}</p>}
      {step.delivery && <p className="obs-muted">投递状态来自此消息的交付记录 · {dateTime(step.delivery.observedAt)}</p>}
      {event.data?.relation === "background_task" && <Alert type="info" content={"后台任务 " + String(event.data.related_task_id) + " · " + String(event.data.related_task_state ?? "状态未记录") + "。独立执行过程尚未接入。"} />}
      {!process.supported && <div className="obs-step-payloads">{payloadEvents.map((item) => <DetailScope key={item.seq} id={`event:${item.seq}`}><section>
        {item.body_ref || item.body_state && item.body_state !== "not_recorded" ?
          <EventBody event={item} scope={scope} title={item.phase === "start" ? "调用参数" : item.phase === "finish" ? "调用结果" : "阶段数据"} /> :
          <><h4>{item.phase === "start" ? "调用输入" : item.phase === "finish" ? "调用结果" : "阶段数据"}</h4>
            <ConfigFields value={visibleMetadata(item)} />
            {item.kind.startsWith("LlmCall") && <p className="obs-muted">{item.phase === "start" ? "输入正文见下方关联上下文。" : "此阶段未记录模型返回正文。"}</p>}</>}
      </section></DetailScope>)}</div>}
      {process.supported && process.panels.filter((panel) => panel.secondary).map((panel) => <Disclosure key={panel.id} stateKey={panel.id} title={panel.title}>
        <ObservationPayload {...scope} reference={panel.event?.body_ref} captureState={panel.event?.body_state}
          title={panel.title} select={panel.select} messages={panel.messages} contentId={panel.id} />
      </Disclosure>)}
      {!process.supported && step.contexts.map((context) => <Disclosure key={context.seq} stateKey={`context:${context.seq}`} title="上下文">
        <EventBody event={context} scope={scope} title="模型可见上下文" />
      </Disclosure>)}
      {event.kind.startsWith("LlmCall") && !step.contexts.length && <p className="obs-muted">未记录可关联的上下文快照</p>}
      {(boundary.entity_id || boundary.refs?.length) && <ExecutionConfiguration {...scope} event={boundary} />}
      {!!step.logs.length && <section aria-label="步骤日志"><h4>步骤日志</h4><Logs events={step.logs} scope={scope} /></section>}
      <Disclosure title="阶段原始记录"><TextPreview text={JSON.stringify({ start: step.start, finish: step.finish, event: !step.start && !step.finish ? event : undefined }, null, 2)} /></Disclosure>
    </div>}
  </article></DetailScope>;
}

function StageCard({ item, terminal, scope, hasMore }: { item: FlowItem; terminal: boolean; scope: BodyScope; hasMore: boolean }) {
  const step = item.step;
  const event = step.event;
  const boundary = step.start ?? event;
  const state = stepState(step, terminal, hasMore);
  const cached = useQuery<ObservationBody>({ queryKey: ["observation-body", scope.instanceId, scope.runId, event.body_ref], enabled: false });
  const body = !scope.expired && cached.data?.payload && typeof cached.data.payload === "object" ? cached.data.payload as Record<string, unknown> : {};
  const error = step.delivery?.errorCode || event.data?.error_code || event.data?.code || body.error;
  const absentStart = hasMore ? "not_loaded" : "not_recorded";
  const absentFinish = terminal ? absentStart : "pending";
  return <DetailScope id={`step:${step.key}`}><article className="obs-runtime-stage" data-step-key={step.key}
    data-runtime-layer={item.layer} data-operation={item.operation} data-status={state.status}>
    <header className="obs-stage-heading"><div><span className="obs-stage-layer">{item.layer && RUNTIME_LAYERS[item.layer]}</span>
      <strong>{RUNTIME_OPERATIONS[item.operation ?? ""] ?? item.operation}</strong><Tag size="small" color={state.color}>{state.label}</Tag></div>
      <div className="obs-stage-facts"><time>{dateTime(step.start?.created_at ?? event.created_at)}</time><strong>{duration(stepDuration(step, terminal))}</strong></div></header>
    {route(boundary) && <p className="obs-step-route">{route(boundary)}</p>}
    {item.operation === "gateway.accept" && boundary.data?.entrypoint === "client" &&
      <p className="obs-muted">渠道适配未经过 · 本任务通过本地协议入口受理。</p>}
    {!!error && <p className="obs-step-error">{errorText(error)}</p>}
    {step.missingParent && <p className="obs-muted">{hasMore ? "上级阶段尚未取得。" : "上级阶段未记录。"}</p>}
    <div className="obs-step-payloads obs-stage-payloads">
      <ObservationPayload {...scope} reference={step.start?.body_ref} captureState={step.start ? step.start.body_state : absentStart} title="输入" bodyField="input" />
      <ObservationPayload {...scope} reference={step.finish?.body_ref} captureState={step.finish ? step.finish.body_state : absentFinish} title="输出" bodyField="output" />
    </div>
    {!!step.permissions.length && <Permissions events={step.permissions} scope={scope} />}
    {step.contexts.map((context) => <Disclosure key={context.seq} stateKey={`context:${context.seq}`} title="上下文"><EventBody event={context} scope={scope} title="模型可见上下文" /></Disclosure>)}
    {(boundary.entity_id || boundary.refs?.length) && <Disclosure title="执行时配置"><ExecutionConfiguration {...scope} event={boundary} /></Disclosure>}
    {!!step.logs.length && <section aria-label="阶段日志"><h4>阶段日志</h4><Logs events={step.logs} scope={scope} /></section>}
    <Disclosure title="阶段原始记录"><TextPreview text={JSON.stringify({ start: step.start, finish: step.finish }, null, 2)} /></Disclosure>
  </article></DetailScope>;
}

function loadExpanded(key: string): Record<string, boolean> {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ?
      Object.fromEntries(Object.entries(value).filter(([, item]) => typeof item === "boolean")) as Record<string, boolean> : {};
  } catch { return {}; }
}

export default function RunInspector({ instanceId, detail, events, visible, onMore, hasMore, fetchingMore }: {
  instanceId: string; detail: GatewayRunDetail; events: GatewayObservation[]; visible: boolean;
  onMore: () => void; hasMore: boolean; fetchingMore: boolean;
}) {
  const run = detail.run;
  const storageKey = "obs:steps:" + instanceId + ":" + run.run_id;
  const [expanded, setExpanded] = useState(() => loadExpanded(storageKey));
  const [, tick] = useState(0);
  const view = useMemo(() => buildRunView(events, detail), [events, detail]);
  const stageCount = view.flow.filter((item) => item.kind === "stage").length;
  const terminal = ["completed", "failed", "aborted"].includes(run.state);
  const scope: BodyScope = { instanceId, runId: run.run_id, expired: !!run.details_expired, active: visible };
  useEffect(() => { setExpanded((current) => rememberOpenedSteps(view.steps, current, terminal)); }, [view.steps, terminal]);
  useEffect(() => { try { sessionStorage.setItem(storageKey, JSON.stringify(expanded)); } catch { /* Browser storage can be disabled. */ } }, [expanded, storageKey]);
  useEffect(() => {
    if (terminal || !visible) return;
    const timer = window.setInterval(() => tick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [terminal, visible]);
  const toggle = (key: string, value: boolean) => setExpanded((current) => ({ ...current, [key]: value }));
  let stepIndex = 0;
  return <TaskDetailState instanceId={instanceId} runId={run.run_id}><section className="obs-run" aria-label="任务运行过程">
    <header className="obs-run-heading"><div><strong>任务运行</strong><code title={run.run_id}>{run.run_id}</code></div>
      <Tag color={runState(run.state).color}>{runState(run.state).label}</Tag></header>
    <div className="obs-run-meta"><span>开始 {dateTime(run.started_at ?? run.created_at)}</span><span>耗时 {duration(runDuration(run))}</span>
      <span>{run.backend ?? "Backend 未记录"} · {run.model ?? "模型未记录"}</span><span>角色 {run.role ?? "未记录"}</span>
      <span>配置 {(run.config_revision ?? run.config_id)?.slice(0, 10) ?? "未记录"}</span>
      <span>实际 Token {run.total_tokens?.toLocaleString() ?? "未记录"}</span></div>
    {!!run.details_expired && <Alert type="info" content="详细正文已到期；任务摘要和阶段指标仍保留。" />}
    {["truncated", "capture_failed"].includes(run.capture_state ?? "") && <Alert type="warning" content={"任务记录" + bodyState(run.capture_state!) + "，部分过程或正文不可用。"} />}
    {(detail.truncated || detail.sanitization_truncated) && <Alert type="warning" content="当前响应包含截断记录，不能视为完整过程。" />}
    <ObservationPayload {...scope} reference={run.input_ref} title="任务输入" />
    <Disclosure title="执行时配置"><ExecutionConfiguration {...scope} /></Disclosure>
    <div className="obs-flow-toolbar"><strong>执行过程 · {stageCount > 0 ? `${stageCount} 段 · ` : ""}{view.flow.length - stageCount} 步{hasMore ? "（还有后续记录）" : ""}</strong><Space size="small">
      <Button size="mini" onClick={() => setExpanded((current) => ({ ...current, ...Object.fromEntries(view.steps.map((step) => [step.key, true])) }))}>展开全部</Button>
      <Button size="mini" onClick={() => setExpanded((current) => ({ ...current, ...Object.fromEntries(view.steps.map((step) => [step.key, false])) }))}>收起全部</Button>
    </Space></div>
    <div className="obs-flow" aria-label="执行时间轴">{view.flow.length ? view.flow.map((item) => item.kind === "stage" ?
      <StageCard key={item.step.key} item={item} terminal={terminal} scope={scope} hasMore={hasMore} /> :
      <StepCard key={item.step.key} item={item} index={stepIndex++} open={stepIsOpen(item.step, expanded, terminal)} terminal={terminal} scope={scope}
        onToggle={toggle} hasMore={hasMore} />) :
      <Empty description={hasMore ? "尚未取得四层运行过程，可继续加载" : "未采集四层运行过程"} />}</div>
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
  </section></TaskDetailState>;
}
