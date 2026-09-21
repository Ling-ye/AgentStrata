import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Tag } from "@arco-design/web-react";
import type { GatewayObservation, GatewayRunDetail } from "./model";
import { DELIVERY_STAGES, layerName, runState } from "./model";
import { bodyState, buildRunView, dateTime, duration, runDuration, stepDuration, stepState, RUNTIME_LAYERS, RUNTIME_OPERATIONS, type DisplayStep, type FlowItem, type ObservationBody } from "./workbenchModel";
import { ConfigFields, DetailScope, Disclosure, ObservationPayload, TaskDetailState, TextPreview } from "./ObservationContent";
import ExecutionConfiguration from "./ExecutionConfiguration";
import TracePanel from "../traces/TracePanel";
import { agentProcess, rawRecordOwners } from "./agentProcessModel";
import { JsonData } from "./StructuredData";
import AgentStreamContent from "./AgentStreamContent";
import ExecutionWorkspace from "./ExecutionWorkspace";
import { executionTitle } from "./executionModel";

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
function EventBody({ event, title, scope, showRaw = true, showTitle = true }: {
  event: GatewayObservation; title: string; scope: BodyScope; showRaw?: boolean; showTitle?: boolean;
}) {
  return <ObservationPayload {...scope} reference={event.body_ref} captureState={event.body_state} title={title} showRaw={showRaw} showTitle={showTitle} />;
}
function supplementaryRecords(step: DisplayStep, contexts: boolean) {
  return [
    ...(contexts ? step.contexts.map((event) => ({ id: `context:${event.seq}`, event, secondary: true })) : []),
    ...step.permissions.map((event) => ({ id: `permission:${event.seq}`, event, secondary: true })),
    ...step.logs.map((event) => ({ id: `log:${event.seq}`, event, secondary: true })),
  ];
}
function Permissions({ events, scope, rawOwners = rawRecordOwners(events.map((event) => ({ id: `permission:${event.seq}`, event }))) }: {
  events: GatewayObservation[]; scope: BodyScope; rawOwners?: ReadonlySet<string>;
}) {
  return <>{events.map((event) => <div key={event.seq} className="obs-permission">
    <div className="obs-permission-summary"><Tag color={event.data?.allowed === true ? "green" : event.data?.allowed === false ? "red" : "gray"}>
      {event.data?.allowed === true ? "允许" : event.data?.allowed === false ? "拒绝" : "决定未记录"}</Tag>
      <span>{String(event.data?.name ?? "")}</span><span>{event.data?.phase === "execution" ? "执行检查" : event.data?.phase === "visibility" ? "可见性检查" : "检查阶段未记录"}</span>
      <code>{String(event.data?.code ?? "")}</code></div>
    <Disclosure stateKey={`permission:${event.seq}`} title="权限记录"><EventBody event={event} title="权限详情" scope={scope}
      showTitle={false} showRaw={rawOwners.has(`permission:${event.seq}`)} /></Disclosure>
  </div>)}</>;
}
function Logs({ events, scope, rawOwners = rawRecordOwners(events.map((event) => ({ id: `log:${event.seq}`, event }))) }: {
  events: GatewayObservation[]; scope: BodyScope; rawOwners?: ReadonlySet<string>;
}) {
  return <>{events.map((event) => <Disclosure key={event.seq} stateKey={`log:${event.seq}`} title={String(event.data?.level ?? "INFO") + " · " + dateTime(event.created_at) + " · " + String(event.data?.logger ?? "")}>
    <EventBody event={event} title="日志正文" scope={scope} showRaw={rawOwners.has(`log:${event.seq}`)} />
  </Disclosure>)}</>;
}

function route(event: GatewayObservation) {
  const source = event.data?.source ?? event.source;
  const target = event.data?.target ?? event.target;
  const name = (value: string) => RUNTIME_LAYERS[value as keyof typeof RUNTIME_LAYERS] ?? layerName(value);
  return typeof source === "string" && typeof target === "string" ? name(source) + " → " + name(target) : "";
}

function StepCard({ item, index, terminal, scope, hasMore }: {
  item: FlowItem; index: number; terminal: boolean; scope: BodyScope; hasMore: boolean;
}) {
  const step = item.step;
  const event = step.event;
  const cached = useQuery<ObservationBody>({ queryKey: ["observation-body", scope.instanceId, scope.runId, event.body_ref], enabled: false });
  const body = !scope.expired && cached.data?.payload && typeof cached.data.payload === "object" ?
    cached.data.payload as Record<string, unknown> : {};
  const error = errorText(step.delivery?.errorCode || body.error || body.error_code || body.message && event.status === "failed" && body.message || event.data?.code);
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
  const panels = process.panels.filter((panel) => panel.secondary || step.finish || !step.deltas?.length || panel.id === "input");
  const rawOwners = rawRecordOwners([
    ...(process.supported ? panels : payloadEvents.map((event) => ({ id: `event:${event.seq}`, event }))),
    ...supplementaryRecords(step, !process.supported),
  ]);
  return <DetailScope id={`step:${step.key}`}><article data-step-key={step.key} data-entity-id={event.entity_id} data-status={state.status}
    data-runtime-layer={item.layer} data-stage-key={item.stageKey} data-process-kind={process.kind}
    className={"obs-step-card" + (state.status === "failed" ? " is-failed" : "")}
    style={{ "--step-depth": Math.min(step.depth, 6) } as CSSProperties}>
    <div className="obs-step-header">
      <div className="obs-step-toggle">
        <span className="obs-step-dot" aria-hidden />
        <span className="obs-step-main"><span className="obs-step-title"><span className="obs-step-index">{String(index + 1).padStart(2, "0")}</span><strong>{executionTitle(item)}</strong><Tag size="small" color={state.color}>{state.label}</Tag></span>
          {!process.supported && <span className="obs-step-route">{item.layer && <b>{RUNTIME_LAYERS[item.layer]} · </b>}{route(boundary) || (!item.layer ? "职责归属未记录" : "层内调用")}</span>}
          {summary && <span className="obs-step-summary">{summary}</span>}
          {process.supported && <span className="obs-step-route">{step.agentName}{process.source ? " · " + process.source : ""}{event.data?.runtime_id ? " · " + String(event.data.runtime_id) : ""}
            {step.modelIteration != null ? ` · 来自第 ${step.modelIteration + 1} 轮模型调用` : ""}</span>}
          {error && <span className="obs-step-error">{error}</span>}
          {step.finish && !step.start && !process.message && <span className="obs-step-gap">{hasMore ? "开始记录尚未取得" : "开始未记录"}</span>}
        </span>
        <span className="obs-step-facts"><span>{dateTime(step.start?.created_at ?? event.created_at)}</span><strong>{duration(elapsed)}</strong>
          {tokens != null && <span>实际 {tokens.toLocaleString()} Token</span>}
          {typeof estimated === "number" && estimated > 0 && <span>输入估算 {estimated.toLocaleString()} Token</span>}</span>
      </div>
    </div>
    {!step.finish && !!step.deltas?.length && <AgentStreamContent {...scope} expired={!!scope.expired} events={step.deltas} />}
    {process.supported && <div className="obs-step-payloads obs-process-panels">{panels.filter((panel) => !panel.secondary).map((panel) =>
      <DetailScope id={panel.id} key={panel.id}><ObservationPayload {...scope}
        reference={panel.event?.body_ref} captureState={panel.event?.body_state ?? (!terminal && panel.id === "output" ? "pending" : "not_recorded")}
        title={panel.title} select={panel.select} messages={panel.messages} contentId={panel.id} showRaw={rawOwners.has(panel.id)} />
      </DetailScope>)}</div>}
    {step.permissions.length > 0 && <div className="obs-step-permissions"><Permissions events={step.permissions} scope={scope} rawOwners={rawOwners} /></div>}
    <div className="obs-step-body" id={"obs-step-body-" + event.seq}>
      {step.missingParent && <p className="obs-muted">{hasMore ? "上级调用尚未取得，还有后续记录可加载。" : "上级调用未采集，本步骤的输入、结果和状态可独立查看。"}</p>}
      {item.missingStage && <p className="obs-muted">{hasMore ? "所属阶段尚未取得。" : "所属阶段未记录，本步骤保留原始关联。"}</p>}
      {step.delivery && <p className="obs-muted">投递状态来自此消息的交付记录 · {dateTime(step.delivery.observedAt)}</p>}
      {event.data?.relation === "background_task" && <Alert type="info" content={"后台任务 " + String(event.data.related_task_id) + " · " + String(event.data.related_task_state ?? "状态未记录") + "。独立执行过程尚未接入。"} />}
      {!process.supported && <div className="obs-step-payloads">{payloadEvents.map((item) => <DetailScope key={item.seq} id={`event:${item.seq}`}><section>
        {item.body_ref || item.body_state && item.body_state !== "not_recorded" ?
          <EventBody event={item} scope={scope} showRaw={rawOwners.has(`event:${item.seq}`)} title={item.phase === "start" ? "调用参数" : item.phase === "finish" ? "调用结果" : "阶段数据"} /> :
          <><h4>{item.phase === "start" ? "调用输入" : item.phase === "finish" ? "调用结果" : "阶段数据"}</h4>
            <ConfigFields value={visibleMetadata(item)} />
            {item.kind.startsWith("LlmCall") && <p className="obs-muted">{item.phase === "start" ? "输入正文见下方关联上下文。" : "此阶段未记录模型返回正文。"}</p>}</>}
      </section></DetailScope>)}</div>}
      {process.supported && panels.filter((panel) => panel.secondary).map((panel) => <Disclosure key={panel.id} stateKey={panel.id} title={panel.title}>
        <ObservationPayload {...scope} reference={panel.event?.body_ref} captureState={panel.event?.body_state}
          title={panel.title} select={panel.select} messages={panel.messages} contentId={panel.id} showTitle={false} showRaw={rawOwners.has(panel.id)} />
      </Disclosure>)}
      {!process.supported && step.contexts.map((context) => <Disclosure key={context.seq} stateKey={`context:${context.seq}`} title="上下文">
        <EventBody event={context} scope={scope} title="模型可见上下文" showRaw={rawOwners.has(`context:${context.seq}`)} />
      </Disclosure>)}
      {event.kind.startsWith("LlmCall") && !step.contexts.length && <p className="obs-muted">未记录可关联的上下文快照</p>}
      {(boundary.entity_id || boundary.refs?.length) && <ExecutionConfiguration {...scope} event={boundary} />}
      {!!step.logs.length && <section aria-label="步骤日志"><h4>步骤日志</h4><Logs events={step.logs} scope={scope} rawOwners={rawOwners} /></section>}
      <Disclosure title="事件元数据"><JsonData value={{ start: step.start, finish: step.finish, event: !step.start && !step.finish ? event : undefined }} /></Disclosure>
    </div>
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
  const rawOwners = rawRecordOwners([{ id: "input", event: step.start }, { id: "output", event: step.finish }, ...supplementaryRecords(step, true)]);
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
      <ObservationPayload {...scope} reference={step.start?.body_ref} captureState={step.start ? step.start.body_state : absentStart} title="输入" bodyField="input" showRaw={rawOwners.has("input")} />
      <ObservationPayload {...scope} reference={step.finish?.body_ref} captureState={step.finish ? step.finish.body_state : absentFinish} title="输出" bodyField="output" showRaw={rawOwners.has("output")} />
    </div>
    {!!step.permissions.length && <Permissions events={step.permissions} scope={scope} rawOwners={rawOwners} />}
    {step.contexts.map((context) => <Disclosure key={context.seq} stateKey={`context:${context.seq}`} title="上下文"><EventBody event={context} scope={scope} title="模型可见上下文" showRaw={rawOwners.has(`context:${context.seq}`)} /></Disclosure>)}
    {(boundary.entity_id || boundary.refs?.length) && <Disclosure title="执行时配置"><ExecutionConfiguration {...scope} event={boundary} showTitle={false} /></Disclosure>}
    {!!step.logs.length && <section aria-label="阶段日志"><h4>阶段日志</h4><Logs events={step.logs} scope={scope} rawOwners={rawOwners} /></section>}
    <Disclosure title="事件元数据"><JsonData value={{ start: step.start, finish: step.finish }} /></Disclosure>
  </article></DetailScope>;
}

export default function RunInspector({ instanceId, detail, events, visible, onMore, hasMore, fetchingMore }: {
  instanceId: string; detail: GatewayRunDetail; events: GatewayObservation[]; visible: boolean;
  onMore: () => void; hasMore: boolean; fetchingMore: boolean;
}) {
  const run = detail.run;
  const [, tick] = useState(0);
  const view = useMemo(() => buildRunView(events, detail), [events, detail]);
  const terminal = ["completed", "failed", "aborted"].includes(run.state);
  const scope: BodyScope = { instanceId, runId: run.run_id, expired: !!run.details_expired, active: visible };
  useEffect(() => {
    if (terminal || !visible) return;
    const timer = window.setInterval(() => tick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [terminal, visible]);
  return <TaskDetailState instanceId={instanceId} runId={run.run_id}><section className="obs-run" aria-label="任务运行过程">
    <header className="obs-run-heading"><div><strong>任务运行</strong><code title={run.run_id}>{run.run_id}</code></div>
      <Tag color={runState(run.state).color}>{runState(run.state).label}</Tag></header>
    <div className="obs-run-meta"><span>开始 {dateTime(run.started_at ?? run.created_at)}</span><span>耗时 {duration(runDuration(run))}</span>
      <span>{run.runtime_id ?? "Backend 未记录"} · {run.model ?? "模型未记录"}</span><span>角色 {run.role ?? "未记录"}</span>
      <span>配置 {(run.config_revision ?? run.config_id)?.slice(0, 10) ?? "未记录"}</span>
      <span>实际 Token {run.total_tokens?.toLocaleString() ?? "未记录"}</span></div>
    {!!run.details_expired && <Alert type="info" content="详细正文已到期；任务摘要和阶段指标仍保留。" />}
    {["truncated", "capture_failed"].includes(run.capture_state ?? "") && <Alert type="warning" content={"任务记录" + bodyState(run.capture_state!) + "，部分过程或正文不可用。"} />}
    {(detail.truncated || detail.sanitization_truncated) && <Alert type="warning" content="当前响应包含截断记录，不能视为完整过程。" />}
    <Disclosure title="任务输入"><ObservationPayload {...scope} reference={run.input_ref} title="任务输入" showTitle={false} /></Disclosure>
    <Disclosure title="执行时配置"><ExecutionConfiguration {...scope} showTitle={false} /></Disclosure>
    <ExecutionWorkspace flow={view.flow} instanceId={instanceId} runId={run.run_id} terminal={terminal} hasMore={hasMore}
      expired={!!run.details_expired} active={visible} renderDetail={(item) => item.kind === "stage" ?
        <StageCard key={item.step.key} item={item} terminal={terminal} scope={scope} hasMore={hasMore} /> :
        <StepCard key={item.step.key} item={item} index={view.steps.findIndex((step) => step.key === item.step.key)} terminal={terminal}
          scope={scope} hasMore={hasMore} />} />
    {hasMore && <Button long loading={fetchingMore} onClick={onMore}>加载后续记录</Button>}
    <TracePanel key={run.run_id} endpoint={`/api/bots/${encodeURIComponent(instanceId)}/gateway-observation/runs/${encodeURIComponent(run.run_id)}/trace`} active={!terminal && visible} title="原始执行记录" />
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
      {!!view.permissions.length && <Disclosure title="任务权限记录">
        <p className="obs-muted">以下记录未绑定到具体调用。</p><Permissions events={view.permissions} scope={scope} /></Disclosure>}
      {!!view.logs.length && <section aria-label="任务日志"><h4>任务日志</h4><Logs events={view.logs} scope={scope} /></section>}
      <Disclosure title="任务状态记录"><ConfigFields value={{ "采集状态": bodyState(run.capture_state ?? "not_recorded"),
        "结束时间": dateTime(run.finished_at), "详情到期": dateTime(run.details_expires_at) }} />
        <TextPreview text={JSON.stringify(view.states, null, 2)} /></Disclosure>
    </section>
  </section></TaskDetailState>;
}
