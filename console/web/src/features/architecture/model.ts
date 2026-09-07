import type { BotInventory, CatalogItem, InfraService } from "../../types";

export const LAYERS = [
  { id: "control", name: "部署与服务" },
  { id: "gateway", name: "Gateway 与协议" },
  { id: "application", name: "应用与上下文" },
  { id: "agent", name: "Agent 与模型" },
  { id: "channel", name: "Channel 与平台" },
  { id: "authorization", name: "身份与权限" },
  { id: "capability", name: "工具与插件" },
  { id: "botspec", name: "实例配置" },
  { id: "contracts", name: "协议与数据兼容" },
] as const;
export type LayerId = typeof LAYERS[number]["id"];

export function layerName(id: string) {
  return LAYERS.find((layer) => layer.id === id)?.name ?? ({ model: "模型 Provider", delivery: "回复交付", transport: "外部传输", middleware: "ACP 中间件" }[id] || id);
}

export function catalogLayer(item: CatalogItem): LayerId {
  if (item.kind === "tool_pack" || item.kind === "mcp" || item.kind === "tool_feature") return "capability";
  if (item.kind === "subagent" || item.kind === "workflow" || item.kind === "prompt") return "agent";
  return "application";
}

export function inventoryUses(item: CatalogItem, inventory: BotInventory) {
  const id = item.id.slice(item.id.indexOf(":") + 1);
  switch (item.kind) {
    case "tool_pack": return inventory.tool_packs.some((entry) => entry.id === id);
    case "tool_feature": return inventory.tool_features.some((entry) => entry.id === id);
    case "mcp": return inventory.mcp_services.some((entry) => entry.ref === id && entry.enabled);
    case "subagent": return inventory.agent_presets.some((entry) => entry.name === id);
    case "workflow": return inventory.workflows.includes(id);
    default: return false;
  }
}

export function serviceLayer(service: InfraService): LayerId {
  return service.service_type === "standalone" ? "channel" : "capability";
}

export const RUN_STATES: Record<string, { label: string; color: string }> = {
  accepted: { label: "已接收", color: "blue" }, running: { label: "执行中", color: "blue" },
  abort_requested: { label: "取消中", color: "orange" }, recovery_required: { label: "等待恢复", color: "orange" },
  completed: { label: "已完成", color: "green" }, aborted: { label: "已取消", color: "gray" },
  failed: { label: "失败", color: "red" }, succeeded: { label: "已完成", color: "green" },
};
export const runState = (state: string) => RUN_STATES[state] ?? { label: "状态未知", color: "gray" };

export interface GatewayRun {
  run_id: string; state: string; error_code: string | null; created_at: number; started_at: number | null;
  finished_at: number | null; updated_at: number; channel?: string; conversation_kind?: string;
  config_revision?: string; config_id?: string; backend?: string; model?: string; role?: string; capture_state?: string;
  details_expired?: boolean; details_expires_at?: number | null; input_ref?: string; result_ref?: string;
  model_calls?: number; tool_calls?: number; total_tokens?: number | null;
  generation?: number; final_text?: string; final_text_truncated?: boolean;
}
export interface GatewayObservation {
  layer?: string; entity_id?: string; refs?: string[]; trace_id?: string; span_id?: string; parent_span_id?: string;
  phase?: string; elapsed_ms?: number | null; body_ref?: string; body_state?: string;
  input_tokens?: number | null; output_tokens?: number | null; total_tokens?: number | null;
  name?: string; model?: string;
  seq: number; kind: string; source?: string; target?: string; status?: string; created_at: number; data?: Record<string, unknown>;
}
export interface GatewayOverview {
  page?: number; limit?: number; total?: number; has_more?: boolean; legacy?: boolean;
  instance_id: string; source: "gateway_state" | "observation_index"; generated_at: number; truncated: boolean; sanitization_truncated: boolean;
  runs: GatewayRun[]; summary: { total: number; active: number | null; failed_recent: number | null };
  audit: Array<{ allowed: number; code: string; policy_version: string; observed_at: number }>; audit_truncated: boolean;
}
export interface GatewayRunDetail {
  has_more?: boolean; next_cursor?: number; legacy?: boolean;
  instance_id: string; source: "gateway_state" | "observation_index"; generated_at: number; truncated: boolean; sanitization_truncated: boolean;
  run: GatewayRun; observations: GatewayObservation[]; observations_available: boolean;
  events: Array<{ seq: number; event: string; created_at: number; data: Record<string, unknown> }>;
  approvals: Array<{ operation: string; state: string; accepted: number | null; created_at: number; decided_at: number | null }>;
  receipts: Array<{ receipt_id: string; outbound_id: string; stage: string; observed_at: number; error_code: string | null }>;
  outbox: Array<{ outbound_id: string; state: string; error_code: string | null; created_at: number; updated_at: number }>;
}

export const OBSERVATION_NAMES: Record<string, string> = {
  principal_bound: "认证身份已绑定", resources_materialized: "输入资源已准备", actor_execution: "进入 Actor 执行会话",
  actor_returned: "执行会话返回", response_dispatch: "发起 Channel 投递", channel_returned: "Channel 投递返回",
  context_prepared: "上下文已准备", resources_dispatched: "资源已送入模型请求", turn_error: "Agent 回合错误",
  LlmCallStarted: "模型请求", LlmCallFinished: "模型返回", ToolStarted: "工具调用", ToolFinished: "工具返回",
  SpanStarted: "能力活动开始", SpanFinished: "能力活动结束", observations_truncated: "后续诊断记录已截断",
};

export const DELIVERY_STAGES: Record<string, string> = {
  gateway_accepted: "Gateway 接受", provider_submitted: "已提交 Provider", provider_acknowledged: "Provider 已确认",
  delivery_unknown: "交付结果未知", platform_displayed: "平台已显示", user_read: "用户已读", failed: "交付失败",
};

export function deliveryLabel(detail: GatewayRunDetail) {
  const stages = new Set(detail.receipts.map((receipt) => receipt.stage));
  if (stages.has("failed") || detail.outbox?.some((item) => item.state === "failed")) return "存在交付失败记录；其他回执不能证明全部回复送达";
  if (stages.has("delivery_unknown") || detail.outbox?.some((item) => item.state === "delivery_unknown")) return "存在交付结果未知的记录，请逐条查看回执与服务日志";
  if (stages.has("user_read")) return "已有用户已读回执";
  if (stages.has("platform_displayed")) return "已有平台显示回执；没有用户已读回执";
  if (stages.has("provider_acknowledged")) return "已有 Provider 确认回执；平台显示与用户已读尚无回执";
  if (stages.has("provider_submitted")) return "已提交 Provider，等待确认";
  return "尚无 Provider 确认回执；任务完成不能证明消息送达";
}
