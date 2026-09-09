import type { GatewayObservation } from "./model";
import type { DisplayStep } from "./workbenchModel";

export type ProcessPanel = {
  id: string; title: string; event?: GatewayObservation;
  select?: (value: unknown) => unknown; messages?: boolean; secondary?: boolean;
};
const fields = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const select = (key: string) => (value: unknown) => fields(value)[key];
export function structuredText(value: unknown): unknown {
  if (typeof value !== "string" || !/^[\s]*[\[{]/.test(value)) return value;
  try { return JSON.parse(value); } catch { return value; }
}
function response(value: unknown) {
  const raw = fields(value).visible_response;
  if (!raw || typeof raw !== "object") return raw;
  const data = fields(raw);
  const calls = Array.isArray(data.tool_calls) ? data.tool_calls.map((item) => {
    const call = fields(item), fn = fields(call.function);
    return { tool_call_id: call.id, name: fn.name, arguments: structuredText(fn.arguments) };
  }) : undefined;
  return { ...(data.content ? { content: data.content } : {}), ...(calls?.length ? { tool_calls: calls } : {}),
    ...(data.error ? { error: data.error } : {}),
    ...(Array.isArray(data.omitted) && data.omitted.length ? { omitted: data.omitted } : {}) };
}
const ACTIVITIES: Record<string, string> = {
  command: "执行命令", file_change: "文件变更", mcp_tool: "MCP 调用", web_search: "搜索",
  plan: "更新计划", reasoning: "思考活动", provider_omission: "过程采集缺口", subagent: "子 Agent", workflow: "工作流",
};

/** Consumes the shared process contract, never a provider's native event shape. */
export function agentProcess(step: DisplayStep) {
  const event = step.event;
  const kind = String(event.data?.process_kind ?? "");
  const name = event.name ?? String(event.data?.name ?? "");
  const model = event.model ?? String(event.data?.model ?? "未记录模型");
  const modelCall = kind === "model_call" || event.kind.startsWith("LlmCall") && kind !== "backend_execution";
  const execution = kind === "backend_execution";
  const catalog = kind === "tool_catalog";
  const tool = kind === "tool" || event.kind === "ToolStarted" || event.kind === "ToolFinished";
  const message = kind === "message";
  const iteration = event.data?.iteration;
  const title = catalog ? ({ initialized: "MCP 服务初始化", list_response_prepared: "已处理 MCP 工具列表请求", failed: "MCP 工具接入失败" }[String(event.data?.catalog_phase)] ?? "MCP 工具接入") : execution ? "Agent 后端执行 · " + model : modelCall ?
    `第 ${typeof iteration === "number" ? iteration + 1 : "?"} 轮模型调用 · ${model}` : tool ? "工具调用 · " + name :
    message ? ({ progress: "Agent 进度消息", final: "Agent 最终消息", response: "Agent 响应消息" }[String(event.data?.message_kind)] ?? "Agent 消息") :
    ACTIVITIES[kind] ? ACTIVITIES[kind] + (kind === "subagent" || kind === "workflow" ? " · " + name.replace(/^[^:]+:/, "") : "") : name;
  const source = { host: "宿主执行记录", provider: "Provider 活动", adapter: "适配器可见", session_gateway: "Session Gateway 记录" }[String(event.data?.source)] ?? "";
  const panels: ProcessPanel[] = catalog ? [{ id: "catalog", title: "工具接入记录", event, select: (value) => { const data = fields(value); return { "提供工具": data.tools, "来源": "Session Gateway", ...(data.error_code ? { "错误码": data.error_code } : {}) }; } }] : modelCall || execution ? [
    { id: "input", title: execution ? "提交的消息" : "本轮发送的消息", event: step.contexts[0], select: select("effective_messages"), messages: true },
    { id: "output", title: execution ? "执行输出" : "本轮模型响应", event: step.finish ?? step.update, select: response },
    { id: "request", title: "请求参数", event: step.start, select: select("request_parameters"), secondary: true },
    { id: "context", title: "上下文与工具定义", event: step.contexts[0], select: (v) => Object.fromEntries(Object.entries(fields(v)).filter(([k]) => k !== "effective_messages")), secondary: true },
  ] : tool ? [
    { id: "input", title: "调用参数", event: step.start, select: select("arguments") },
    { id: "output", title: "工具执行结果", event: step.finish, select: (v) => {
      const data = fields(v); return { summary: data.summary, result: data.execution_result ?? data.data, error: data.error };
    } },
    { id: "model-result", title: "实际交给模型的结果", event: step.finish, select: select("model_result"), secondary: true, messages: true },
  ] : message ? [{ id: "message", title: "消息正文", event, select: select("text") }] : [
    { id: "input", title: kind === "subagent" ? "委托输入" : "输入", event: step.start ?? step.update ?? step.finish, select: select("input") },
    { id: "output", title: kind === "subagent" ? "委托结果" : "输出", event: step.finish ?? step.update,
      select: (v) => {
        const data = fields(v);
        if (data.output != null || data.result != null || data.data != null) return data.output ?? data.result ?? data.data;
        const remainder = Object.fromEntries(Object.entries(data).filter(([key, value]) =>
          !["input", "output", "configuration", "capture_state"].includes(key) && value != null));
        return Object.keys(remainder).length ? remainder : null;
      } },
  ];
  if (kind === "subagent") panels.push({ id: "configuration", title: "委托配置", event: step.start, select: select("configuration"), secondary: true });
  return { title, kind, source, panels, message, modelCall, execution,
    supported: catalog || !!ACTIVITIES[kind] || modelCall || execution || tool || message || !!name };
}

export function processPreview(value: unknown): string {
  if (value == null) return "未提供正文";
  if (typeof value === "string") return value.slice(0, 360) || "已留空";
  if (Array.isArray(value)) {
    const last = value[value.length - 1];
    if (last && typeof last === "object" && "role" in last) return processPreview(last);
    return value.slice(-3).map(processPreview).join("\n").slice(0, 360) || "无";
  }
  if (typeof value !== "object") return String(value);
  const data = fields(value);
  if (typeof data.content === "string" && data.content) return data.content.slice(0, 360);
  return Object.entries(data).filter(([, v]) => v != null && v !== "").slice(0, 6)
    .map(([key, v]) => `${key}: ${processPreview(v)}`).join("\n").slice(0, 360) || "未提供正文";
}
