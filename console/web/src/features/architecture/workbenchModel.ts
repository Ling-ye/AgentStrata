import type { GatewayObservation, GatewayRun, GatewayRunDetail } from "./model";
import { runState } from "./model";

export interface InspectionEntity {
  id: string; layer: string; name: string; refs?: string[]; runtime_stale?: boolean;
  configured: boolean | null; loaded: boolean | null; connected: boolean | null; available: boolean | null;
  source_config?: Record<string, unknown> | null; source_environment?: Record<string, unknown>;
  effective_environment?: Record<string, unknown>; effective_config?: Record<string, unknown> | null;
  config: Record<string, unknown> | null; runtime?: Record<string, unknown>; environment?: Record<string, unknown>;
}
export interface Configuration {
  layers: Array<{ id: string; name: string }>; entities: InspectionEntity[];
  configuration_revision?: string; backend?: string; model?: string;
  environment_revision?: string; visibility?: "operator";
  capture_state?: string;
  validation?: Array<{ field: string; level: string; message: string }>;
}
export interface Inspection {
  current: Configuration | null; loaded: Configuration | null; execution: Configuration | null;
  loaded_meta: { config_id: string; observed_at: number; generation: number } | null;
  configuration_status?: "applied" | "pending" | "unknown"; configuration_status_reason?: string;
  loaded_stale: boolean; pending_changes: boolean; generated_at: number;
  sanitization_truncated?: boolean;
  errors: Array<{ source: string; code: string; message: string }>;
}
export interface ObservationFilters {
  since?: number; until?: number; state?: string; config_id?: string; backend?: string; model?: string;
  component?: string; error_code?: string; search?: string; min_ms?: number; page?: number; limit?: number;
}
export interface EventPage {
  observations: GatewayObservation[]; next_cursor: number; has_more: boolean;
}
export interface ObservationBody { body_id: string; state: string; payload: unknown }
export interface ObservationMetrics {
  totals: Record<string, number | null>;
  components_truncated?: boolean; trends_truncated?: boolean;
  components: Array<{ layer: string; entity_id: string; calls: number; failures: number; mean_ms: number | null; timing_samples: number }>;
  trends: Array<{ day: string; config_id: string | null; backend: string | null; model: string | null; total: number; failed: number; mean_ms: number | null; sample_count: number; p50_ms: number | null; p95_ms: number | null }>;
  since: number | null; until: number | null; generated_at: number;
}
export interface RunStep {
  key: string; event: GatewayObservation; start?: GatewayObservation; finish?: GatewayObservation;
  children: RunStep[]; missingParent: boolean;
}

export function observationQuery(filters: ObservationFilters = {}) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  return params.size ? `?${params}` : "";
}

export function buildRunTree(events: GatewayObservation[]): RunStep[] {
  const nodes = new Map<string, RunStep>();
  const spanKey = (trace?: string, span?: string) => trace && span ? JSON.stringify([trace, span]) : undefined;
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const key = (event.phase === "start" || event.phase === "finish") && spanKey(event.trace_id, event.span_id) || `event:${event.seq}`;
    const node = nodes.get(key) ?? { key, event, children: [], missingParent: false };
    if (event.phase === "start") node.start = event;
    if (event.phase === "finish") node.finish = event;
    node.event = node.finish ?? node.start ?? event;
    nodes.set(key, node);
  }
  const roots: RunStep[] = [];
  for (const node of nodes.values()) {
    const parentId = node.event.parent_span_id ?? node.start?.parent_span_id;
    const parentKey = spanKey(node.event.trace_id, parentId);
    const parent = parentKey ? nodes.get(parentKey) : undefined;
    // Corrupt parent cycles stay visible at the root instead of recursing indefinitely.
    let cursor = parent;
    const seen = new Set([node.key]);
    while (cursor && !seen.has(cursor.key)) {
      seen.add(cursor.key);
      const next = spanKey(cursor.event.trace_id, cursor.event.parent_span_id ?? cursor.start?.parent_span_id);
      cursor = next ? nodes.get(next) : undefined;
    }
    if (parent && !cursor) parent.children.push(node);
    else {
      node.missingParent = !!parentId;
      roots.push(node);
    }
  }
  return roots;
}

export interface DisplayStep extends RunStep {
  depth: number; contexts: GatewayObservation[]; permissions: GatewayObservation[]; logs: GatewayObservation[];
  delivery?: { status: string; observedAt: number; errorCode?: string | null };
}

export const RUNTIME_LAYERS = { channel: "渠道适配", gateway: "网关", application: "应用", agent: "Agent" } as const;
export type RuntimeLayer = keyof typeof RUNTIME_LAYERS;
export const RUNTIME_OPERATIONS: Record<string, string> = {
  "channel.receive": "接收消息", "gateway.accept": "准入与受理", "application.prepare": "准备工作区与资源",
  "application.session": "准备执行会话", "agent.execute": "Agent 执行", "application.result": "接收执行结果",
  "gateway.dispatch": "准备回复投递", "channel.deliver": "投递消息", "gateway.delivery": "记录交付结果",
  "application.exchange": "处理会话交换", "gateway.finish": "结束任务",
};
function runtimeLayer(value: unknown): RuntimeLayer | undefined {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(RUNTIME_LAYERS, value) ? value as RuntimeLayer : undefined;
}
export function stepRuntimeLayer(step: RunStep): RuntimeLayer | undefined {
  const event = step.start ?? step.event;
  if (event.data?.flow_version === 1) return runtimeLayer(event.data.runtime_layer);
  return undefined;
}
export function runtimeStage(event: GatewayObservation) {
  if (!["RuntimeStageStarted", "RuntimeStageFinished"].includes(event.kind) || event.data?.flow_version !== 1 ||
      !event.trace_id || !event.span_id) return undefined;
  const layer = runtimeLayer(event.data.runtime_layer);
  const operation = event.data.operation;
  return layer && typeof operation === "string" && operation ? { layer, operation } : undefined;
}
export interface FlowItem {
  kind: "stage" | "step"; step: DisplayStep; layer?: RuntimeLayer; operation?: string;
  stageKey?: string; missingStage: boolean;
}

function buildFlowItems(steps: DisplayStep[]): FlowItem[] {
  const stageSteps = steps.filter((step) => runtimeStage(step.start ?? step.event));
  if (!stageSteps.length) return steps.map((step) => ({ kind: "step", step, layer: stepRuntimeLayer(step),
    missingStage: typeof (step.start ?? step.event).data?.stage_span_id === "string" }));
  const byKey = new Map(steps.map((step) => [step.key, step]));
  const stageKeys = new Set(stageSteps.map((step) => step.key));
  const stageFor = (step: DisplayStep): string | undefined => {
    const event = step.start ?? step.event;
    const explicit = event.data?.stage_span_id ?? step.finish?.data?.stage_span_id;
    if (typeof explicit === "string" && event.trace_id) return JSON.stringify([event.trace_id, explicit]);
    let cursor: DisplayStep | undefined = step;
    const seen = new Set<string>();
    while (cursor && !seen.has(cursor.key)) {
      seen.add(cursor.key);
      const parent: string | undefined = cursor.event.parent_span_id ?? cursor.start?.parent_span_id;
      const key: string | undefined = parent && cursor.event.trace_id ? JSON.stringify([cursor.event.trace_id, parent]) : undefined;
      if (key && stageKeys.has(key)) return key;
      cursor = key ? byKey.get(key) : undefined;
    }
    return undefined;
  };
  const blocks: Array<{ seq: number; items: FlowItem[] }> = [];
  const content = new Map<string, FlowItem[]>();
  for (const step of stageSteps) {
    const info = runtimeStage(step.start ?? step.event)!;
    const items: FlowItem[] = [{ kind: "stage", step: { ...step, depth: 0 }, ...info, missingStage: false }];
    content.set(step.key, items);
    blocks.push({ seq: step.start?.seq ?? step.event.seq, items });
  }
  for (const step of steps) {
    if (stageKeys.has(step.key)) continue;
    const stageKey = stageFor(step);
    const parentStage = stageKey && byKey.get(stageKey);
    const item: FlowItem = { kind: "step", step: parentStage && stageKeys.has(parentStage.key) ?
      { ...step, depth: Math.max(0, step.depth - parentStage.depth - 1) } : step,
      layer: stepRuntimeLayer(step), stageKey, missingStage: !!stageKey && !stageKeys.has(stageKey) };
    const block = stageKey && content.get(stageKey);
    if (block) block.push(item);
    else blocks.push({ seq: step.start?.seq ?? step.event.seq, items: [item] });
  }
  return blocks.sort((a, b) => a.seq - b.seq).flatMap((block) => block.items);
}

export function buildRunView(events: GatewayObservation[], delivery?: Pick<GatewayRunDetail, "receipts" | "outbox">) {
  const unique = [...new Map(events.map((event) => [event.seq, event])).values()].sort((a, b) => a.seq - b.seq);
  const supplemental = new Set(["ContextSnapshotPrepared", "tool_authorization", "log", "run_state"]);
  const steps: DisplayStep[] = [];
  const flatten = (nodes: RunStep[], depth = 0) => nodes.forEach((node) => {
    steps.push({ ...node, depth, contexts: [], permissions: [], logs: [] });
    flatten(node.children, depth + 1);
  });
  const current = unique.filter((event) => event.data?.flow_version === 1);
  const contextual = current.filter((event) => event.kind === "ContextSnapshotPrepared");
  const core = current.filter((event) => !supplemental.has(event.kind));
  const usedContexts = new Set<number>();
  const contextsByCall = new Map<string, GatewayObservation[]>();
  for (const event of core) {
    const id = event.data?.context_snapshot_id;
    if (!id || !event.trace_id || !event.span_id) continue;
    const matches = contextual.filter((context) => context.data?.snapshot_id === id &&
      (!context.trace_id || context.trace_id === event.trace_id));
    if (matches.length === 1) {
      contextsByCall.set(JSON.stringify([event.trace_id, event.span_id]), matches);
      usedContexts.add(matches[0].seq);
    }
  }
  flatten(buildRunTree([...core, ...contextual.filter((event) => !usedContexts.has(event.seq))]));
  for (const step of steps) {
    step.contexts = contextsByCall.get(JSON.stringify([step.event.trace_id, step.event.span_id])) ?? [];
    if ((step.start ?? step.event).data?.operation !== "channel.deliver") continue;
    const outbound = step.event.data?.outbound_id ?? step.start?.data?.outbound_id;
    if (typeof outbound !== "string") continue;
    const outbox = delivery?.outbox.find((item) => item.outbound_id === outbound);
    const receipts = delivery?.receipts.filter((item) => item.outbound_id === outbound) ?? [];
    const receipt = receipts[receipts.length - 1];
    if (outbox) step.delivery = { status: outbox.state, observedAt: outbox.updated_at, errorCode: outbox.error_code };
    else if (receipt) step.delivery = { status: receipt.stage, observedAt: receipt.observed_at, errorCode: receipt.error_code };
  }
  const permissions: GatewayObservation[] = [];
  const logs: GatewayObservation[] = [];
  for (const event of unique) {
    if (event.kind !== "tool_authorization" && event.kind !== "log") continue;
    if (event.kind === "tool_authorization" && event.data?.flow_version !== 1) continue;
    const candidates = event.trace_id && event.span_id ? steps.filter((step) =>
      step.event.trace_id === event.trace_id && step.event.span_id === event.span_id &&
      (event.kind !== "tool_authorization" || !event.entity_id || related(step.event, event.entity_id))) : [];
    const collection = event.kind === "log" ? "logs" : "permissions";
    if (candidates.length === 1) candidates[0][collection].push(event);
    else (collection === "logs" ? logs : permissions).push(event);
  }
  const flow = buildFlowItems(steps);
  return { steps, flow, permissions, logs, states: unique.filter((event) => event.kind === "run_state") };
}

export function stepState(step: DisplayStep, terminal: boolean, hasMore = false) {
  const status = step.delivery?.status ?? step.event.status ?? "unknown";
  const ended = ["provider_acknowledged", "platform_displayed", "user_read", "failed", "delivery_unknown"].includes(status);
  const incomplete = terminal && !step.delivery && !ended && !step.finish && (!!step.start || status === "running");
  return { status: incomplete ? "unknown" : status, incomplete,
    ...(incomplete ? { label: hasMore ? "结束记录尚未取得" : "结束未记录", color: "orange" } : runState(status)) };
}

export function stepDuration(step: DisplayStep, terminal: boolean, now = Date.now() / 1000) {
  if ((step.start ?? step.event).data?.duration_recorded === false || step.event.data?.duration_recorded === false) return null;
  return step.event.elapsed_ms ?? (step.start && step.finish ? Math.max(0, step.finish.created_at - step.start.created_at) * 1000 :
    step.start && step.delivery && !["pending", "submitting", "gateway_accepted", "provider_submitted"].includes(step.delivery.status) ?
      (step.delivery.observedAt >= step.start.created_at ? (step.delivery.observedAt - step.start.created_at) * 1000 : null) :
    step.start && !terminal ? Math.max(0, now - step.start.created_at) * 1000 : null);
}

export function stepIsOpen(step: RunStep, overrides: Record<string, boolean>, terminal: boolean) {
  return overrides[step.key] ?? (step.event.status === "failed" || (!terminal && step.event.status === "running"));
}

export function related(event: GatewayObservation, entity: string) {
  return !entity || event.entity_id === entity || event.refs?.includes(entity) === true;
}
export function duration(ms: number | null | undefined) {
  if (ms == null || !Number.isFinite(ms)) return "未记录";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${(ms / 60000).toFixed(1)} min`;
}
export function runDuration(run: GatewayRun, now = Date.now() / 1000) {
  return run.started_at == null ? null : Math.max(0, ((run.finished_at ?? now) - run.started_at) * 1000);
}
export const dateTime = (epoch: number | null | undefined) => epoch == null ? "未记录" : new Date(epoch * 1000).toLocaleString();
export const bodyState = (state: string) => ({ available: "已记录", expired: "已到期", truncated: "已截断", not_recorded: "未采集", capture_failed: "采集失败", recording: "采集中", recorded: "已记录", not_loaded: "尚未取得", pending: "尚未记录输出" }[state] ?? state);
