import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Spin } from "@arco-design/web-react";
import { api } from "../../api";
import { bodyState } from "./workbenchModel";

export const FIELD_NAMES: Record<string, string> = {
  version: "运行版本", ready_at: "就绪时间（Unix 秒）", pid: "进程 PID", backend: "执行后端", model: "模型",
  timeout_seconds: "超时（秒）", max_concurrency: "最大并发", enabled: "启用", requires_role: "最低角色",
  private_chat_only: "仅私聊", read_role: "读取角色", max_results: "最大结果数", reasoning_effort: "推理强度",
  profiles: "模型配置档", code_task_profile: "代码任务配置档", env_prefix: "环境变量前缀", tool_prefix: "工具前缀",
  exposure: "可见范围", transport: "传输方式", tools_count: "工具数量", error: "错误", running: "运行中",
  root_env: "目录环境变量", manifest: "配置清单", allowed_tools: "允许工具", denied_tools: "禁用工具",
  max_tool_calls: "工具调用上限", max_model_turns: "模型轮次上限", policy_version: "策略版本", role: "角色",
  gateway: "Gateway", prompts: "提示词", observation: "观测记录", configured: "已配置", reference: "引用",
  provider: "提供方", kind: "类型", namespace: "命名空间", schema: "数据格式", host: "监听地址", count: "数量",
  memory_store: "记忆存储", wiki: "Wiki", codebases: "代码仓库", playbooks: "Skills", dev: "开发工具", rag: "RAG",
  arguments: "参数", summary: "摘要", data: "结果数据", message: "消息", text: "正文", final_text: "最终回复",
  content: "正文", effective_messages: "模型可见输入", session_messages: "会话历史", tool_schemas: "工具定义",
  resources: "资源", coverage: "采集范围", omitted: "未覆盖内容", context_kind: "上下文类型",
  input_message_count: "输入消息数", input_estimated_tokens: "输入 Token（估算）", estimated_tokens: "Token（估算）",
  input_tokens: "输入 Token", output_tokens: "输出 Token", total_tokens: "实际 Token", cached_tokens: "缓存 Token",
  prompt_tokens: "输入 Token", completion_tokens: "输出 Token", usage: "用量", finish_reason: "结束原因",
  iteration: "调用轮次", code: "原因码", error_code: "错误码", name: "名称", level: "级别", logger: "日志来源",
  ok: "执行成功", model_selection: "模型配置", tool_count: "可用工具数", channel: "通道", state: "状态",
  operation: "操作", accepted: "已接受", created_at: "创建时间（Unix 秒）", decided_at: "决定时间（Unix 秒）",
};

export function TextPreview({ text }: { text: string }) {
  const [full, setFull] = useState(false);
  return <><div className="obs-text-value">{full ? text : text.slice(0, 1200)}</div>
    {text.length > 1200 && <Button size="mini" type="text" onClick={() => setFull(!full)}>
      {full ? "收起正文" : `展开全文（${text.length.toLocaleString()} 字符）`}</Button>}</>;
}

function fieldRows(value: unknown, limit: number, path: string[] = [], rows: Array<[string, unknown]> = []) {
  if (rows.length > limit) return rows;
  if (value && typeof value === "object" && Object.keys(value).length) {
    for (const [key, item] of Object.entries(value)) {
      fieldRows(item, limit, [...path, Array.isArray(value) ? `第 ${Number(key) + 1} 项` : FIELD_NAMES[key] ?? key], rows);
      if (rows.length > limit) break;
    }
  } else rows.push([path.join(" / "), value]);
  return rows;
}

export function ConfigFields({ value }: { value: unknown }) {
  const [limit, setLimit] = useState(60);
  const rows = fieldRows(value, limit);
  return <><dl className="obs-fields">{rows.slice(0, limit).map(([key, item], index) => <div key={`${key}:${index}`}>
    {key && <dt>{key}</dt>}<dd>{item == null ? <span className="obs-muted">未记录</span> :
      <TextPreview text={typeof item === "boolean" ? item ? "是" : "否" : typeof item === "object" ? "无" : String(item)} />}</dd>
  </div>)}</dl>{rows.length > limit && <Button size="small" onClick={() => setLimit(limit + 60)}>显示更多字段</Button>}</>;
}

function ContextMessages({ value }: { value: unknown }) {
  const [limit, setLimit] = useState(20);
  if (!Array.isArray(value)) return <ConfigFields value={value} />;
  if (!value.length) return <p className="obs-muted">未记录消息</p>;
  return <div className="obs-context-messages">{value.slice(0, limit).map((item, index) => {
    const message = item && typeof item === "object" ? item as Record<string, unknown> : null;
    const role = String(message?.role ?? "消息");
    const extra = message ? Object.fromEntries(Object.entries(message).filter(([key]) => !["role", "content"].includes(key))) : {};
    return <article className="obs-context-message" key={index}>
      <header><strong>{({ user: "用户", assistant: "助手", system: "系统", developer: "开发者", tool: "工具" } as Record<string, string>)[role] ?? role}</strong><span>{index + 1}</span></header>
      {typeof message?.content === "string" ? <TextPreview text={message.content} /> : <ConfigFields value={message ? message.content : item} />}
      {!!Object.keys(extra).length && <ConfigFields value={extra} />}
    </article>;
  })}{value.length > limit && <Button size="small" onClick={() => setLimit(limit + 20)}>显示更多消息（剩余 {value.length - limit} 条）</Button>}</div>;
}

function PayloadContent({ value }: { value: unknown }) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return <ConfigFields value={value} />;
  const fields = value as Record<string, unknown>;
  const messageKeys = ["session_messages", "effective_messages"].filter((key) => key in fields);
  if (!messageKeys.length) return <ConfigFields value={value} />;
  const other = Object.fromEntries(Object.entries(fields).filter(([key]) => !messageKeys.includes(key)));
  return <><div className="obs-context-grid">{messageKeys.map((key) => <section key={key}>
    <h4>{FIELD_NAMES[key]}</h4><ContextMessages value={fields[key]} />
  </section>)}</div>{!!Object.keys(other).length && <ConfigFields value={other} />}</>;
}

export function Disclosure({ title, children }: { title: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <details className="obs-disclosure" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{title}</summary>{open && <div className="obs-disclosure-content">{children}</div>}
  </details>;
}

function useInView() {
  const ref = useRef<HTMLElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: "100px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return { ref, visible };
}

const BODY_METADATA = new Set(["name", "trace_id", "span_id", "parent_span_id", "depth", "started_at", "finished_at",
  "snapshot_id", "context_snapshot_id", "iteration", "private_reasoning_omission_count", "resource_path_omission_count"]);
const COVERAGE: Record<string, string> = { exact_model_input: "精确模型输入", adapter_visible: "仅适配器可见部分", partial: "部分采集", provider_opaque: "Provider 未公开" };

export function ObservationPayload({ instanceId, runId, reference, expired = false, captureState, title, active = true }: {
  instanceId: string; runId: string; reference?: string; expired?: boolean; captureState?: string; title: string; active?: boolean;
}) {
  const { ref, visible } = useInView();
  const query = useQuery({ queryKey: ["observation-body", instanceId, runId, reference],
    queryFn: ({ signal }) => api.observationBody(instanceId, runId, reference!, signal),
    enabled: active && visible && !!reference && !expired, staleTime: Infinity, retry: false });
  const state = expired ? "expired" : query.data?.state ?? captureState ?? (reference ? "available" : "not_recorded");
  const payload = query.data?.payload;
  const content = payload && typeof payload === "object" && !Array.isArray(payload) ?
    Object.fromEntries(Object.entries(payload).filter(([key, value]) => !BODY_METADATA.has(key) && value !== "")
      .map(([key, value]) => [key, key === "coverage" && typeof value === "string" ? COVERAGE[value] ?? value : value])) : payload;
  return <section ref={ref} className="obs-payload" aria-label={title}>
    <div className="obs-pane-heading"><strong>{title}</strong><span>{bodyState(state)}</span></div>
    {state === "expired" ? <p className="obs-muted">详细正文已到期，结构化记录仍保留。</p> : query.error ?
      <Alert type="error" content={<span>详情读取失败：{query.error.message} <Button size="mini" onClick={() => void query.refetch()}>重试</Button></span>} /> :
      query.isFetching ? <Spin size={16} /> : payload != null ? <>
        <PayloadContent value={content} />
        <Disclosure title="原始记录"><TextPreview text={JSON.stringify(payload, null, 2)} /></Disclosure>
      </> : <p className="obs-muted">{reference && !visible ? "滚动到此处时加载" : reference ? "等待读取" : bodyState(state)}</p>}
  </section>;
}
