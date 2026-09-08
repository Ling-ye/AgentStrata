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

export function buildRunView(events: GatewayObservation[], delivery?: Pick<GatewayRunDetail, "receipts" | "outbox">) {
  const unique = [...new Map(events.map((event) => [event.seq, event])).values()].sort((a, b) => a.seq - b.seq);
  const supplemental = new Set(["ContextSnapshotPrepared", "tool_authorization", "log", "run_state"]);
  const steps: DisplayStep[] = [];
  const flatten = (nodes: RunStep[], depth = 0) => nodes.forEach((node) => {
    steps.push({ ...node, depth, contexts: [], permissions: [], logs: [] });
    flatten(node.children, depth + 1);
  });
  const contextual = unique.filter((event) => event.kind === "ContextSnapshotPrepared");
  const core = unique.filter((event) => !supplemental.has(event.kind));
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
    if (!["response_dispatch", "channel_returned"].includes(step.event.kind)) continue;
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
    const candidates = event.trace_id && event.span_id ? steps.filter((step) =>
      step.event.trace_id === event.trace_id && step.event.span_id === event.span_id &&
      (event.kind !== "tool_authorization" || !event.entity_id || related(step.event, event.entity_id))) : [];
    const collection = event.kind === "log" ? "logs" : "permissions";
    if (candidates.length === 1) candidates[0][collection].push(event);
    else (collection === "logs" ? logs : permissions).push(event);
  }
  return { steps, permissions, logs, states: unique.filter((event) => event.kind === "run_state") };
}

export function stepState(step: DisplayStep, terminal: boolean) {
  const status = step.delivery?.status ?? step.event.status ?? "unknown";
  const ended = ["provider_acknowledged", "platform_displayed", "user_read", "failed", "delivery_unknown"].includes(status);
  const incomplete = terminal && !step.delivery && !ended && !step.finish && (!!step.start || status === "running");
  return { status: incomplete ? "unknown" : status, incomplete,
    ...(incomplete ? { label: "结束未记录", color: "orange" } : runState(status)) };
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
export const bodyState = (state: string) => ({ available: "已记录", expired: "已到期", truncated: "已截断", not_recorded: "未采集", capture_failed: "采集失败", recording: "采集中", recorded: "已记录" }[state] ?? state);
