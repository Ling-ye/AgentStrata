import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Spin } from "@arco-design/web-react";
import { api } from "../../api";
import { bodyState } from "./workbenchModel";
import { readSessionValue, saveSessionValue } from "./taskWorkspaceState";

type DetailValue = boolean | number;
const DetailState = createContext<{ prefix: string; values: Record<string, DetailValue>; set: (key: string, value: DetailValue) => void } | null>(null);

export function TaskDetailState({ instanceId, runId, children }: { instanceId: string; runId: string; children: ReactNode }) {
  const storageKey = `obs:details:${instanceId}:${runId}`;
  const [values, setValues] = useState<Record<string, DetailValue>>(() => {
    const saved = readSessionValue(storageKey);
    return saved && typeof saved === "object" && !Array.isArray(saved) ? Object.fromEntries(Object.entries(saved)
      .filter(([, value]) => typeof value === "boolean" || typeof value === "number" && Number.isFinite(value) && value >= 0)) : {};
  });
  useEffect(() => saveSessionValue(storageKey, values), [storageKey, values]);
  return <DetailState.Provider value={{ prefix: "task", values,
    set: (key, value) => setValues((current) => current[key] === value ? current : { ...current, [key]: value }) }}>{children}</DetailState.Provider>;
}

export function DetailScope({ id, children }: { id: string; children: ReactNode }) {
  const state = useContext(DetailState);
  return state ? <DetailState.Provider value={{ ...state, prefix: state.prefix + "/" + JSON.stringify(id) }}>{children}</DetailState.Provider> : <>{children}</>;
}

function useDetailValue<T extends DetailValue>(key: string, initial: T): [T, (value: T) => void] {
  const state = useContext(DetailState);
  const [local, setLocal] = useState<T>(initial);
  const fullKey = state?.prefix + "/" + key;
  const saved = state?.values[fullKey];
  return [state && typeof saved === typeof initial ? saved as T : state ? initial : local,
    (value) => { if (state) state.set(fullKey, value); else setLocal(value); }];
}

export const FIELD_NAMES: Record<string, string> = {
  target: "部署环境", cc_connect_config_dir: "Legacy 接入配置目录", project_name: "项目名称", secret_json: "凭据配置引用",
  file_access: "文件访问", isolation: "隔离方式", scope: "作用范围", format: "格式", repository_id: "仓库 ID",
  read_only: "只读", auth: "认证", protocol_version: "协议版本", listen: "监听", credential: "凭据",
  port: "监听端口", state_root: "状态存储目录", token: "认证令牌", account: "账号", ws_url: "连接地址",
  wsl_home: "运行目录", env_file: "环境配置文件", source_spec: "BotSpec 来源", source_root: "源配置目录",
  workspace_root: "工作区目录", log_dir: "日志目录", instance_id: "实例 ID", display_name: "显示名称",
  defaults: "默认预算", agents: "子 Agent 预算", overrides: "覆盖配置", custom: "自定义子 Agent",
  allowed_paths: "允许路径", denied_paths: "禁止路径", shell: "命令执行", timeout_default: "默认超时（秒）", timeout_max: "最大超时（秒）",
  include: "包含规则", exclude: "排除规则", sources: "数据源清单", path: "路径", label: "名称", registry: "仓库清单",
  include_globs: "包含规则", deny_globs: "排除规则", allow_extensions: "文件类型", max_read_bytes: "读取上限（字节）",
  command: "启动命令", args: "启动参数", env: "进程环境", headers: "请求头", url: "连接地址",
  allowed_subagents: "允许的子 Agent", owner_access: "Owner 访问策略", member_access: "成员访问策略",
  require_at_in_group: "群聊需要 @", max_output_chars: "输出长度上限", max_workflow_depth: "Workflow 深度上限",
  identity: "身份提示词", response_style: "回复风格", refusal_style: "拒答风格", role_styles: "角色风格", mode_styles: "模式风格",
  description: "说明", body_path: "内容来源", schema_version: "配置版本", max_chunk_chars: "分块字符上限",
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
  input: "输入", output: "输出", visible_response: "本轮模型公开输出", stop_reason: "结束原因",
  canonical_text: "规范消息正文", segments: "消息内容", tool_calls: "工具调用建议", receipt: "交付回执",
  request_id: "请求 ID", exchange: "会话交换", outcome: "处理结果", native_message: "渠道消息", entrypoint: "任务入口",
};

export function TextPreview({ text, stateKey = "text" }: { text: string; stateKey?: string }) {
  const [full, setFull] = useDetailValue<boolean>(stateKey, false);
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

export function ConfigFields({ value, missingLabel = "未记录" }: { value: unknown; missingLabel?: string }) {
  const [limit, setLimit] = useDetailValue<number>("field-limit", 60);
  const rows = fieldRows(value, limit);
  return <><dl className="obs-fields">{rows.slice(0, limit).map(([key, item], index) => <div key={`${key}:${index}`}>
    {key && <dt>{key}</dt>}<dd>{item == null ? <span className="obs-muted">{missingLabel}</span> :
      item === "" ? <span className="obs-muted">已留空</span> :
      <TextPreview stateKey={`field:${index}`} text={typeof item === "boolean" ? item ? "是" : "否" : typeof item === "object" ? "无" : String(item)} />}</dd>
  </div>)}</dl>{rows.length > limit && <Button size="small" onClick={() => setLimit(limit + 60)}>显示更多字段</Button>}</>;
}

function ContextMessages({ value }: { value: unknown }) {
  const [limit, setLimit] = useDetailValue<number>("message-limit", 20);
  if (!Array.isArray(value)) return <ConfigFields value={value} />;
  if (!value.length) return <p className="obs-muted">未记录消息</p>;
  return <div className="obs-context-messages">{value.slice(0, limit).map((item, index) => {
    const message = item && typeof item === "object" ? item as Record<string, unknown> : null;
    const role = String(message?.role ?? "消息");
    const extra = message ? Object.fromEntries(Object.entries(message).filter(([key]) => !["role", "content"].includes(key))) : {};
    return <DetailScope id={`message:${index}`} key={index}><article className="obs-context-message">
      <header><strong>{({ user: "用户", assistant: "助手", system: "系统", developer: "开发者", tool: "工具" } as Record<string, string>)[role] ?? role}</strong><span>{index + 1}</span></header>
      {typeof message?.content === "string" ? <TextPreview text={message.content} /> : <ConfigFields value={message ? message.content : item} />}
      {!!Object.keys(extra).length && <DetailScope id="extra"><ConfigFields value={extra} /></DetailScope>}
    </article></DetailScope>;
  })}{value.length > limit && <Button size="small" onClick={() => setLimit(limit + 20)}>显示更多消息（剩余 {value.length - limit} 条）</Button>}</div>;
}

function PayloadContent({ value }: { value: unknown }) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return <ConfigFields value={value} />;
  const fields = value as Record<string, unknown>;
  const messageKeys = ["session_messages", "effective_messages"].filter((key) => key in fields);
  if (!messageKeys.length) return <ConfigFields value={value} />;
  const other = Object.fromEntries(Object.entries(fields).filter(([key]) => !messageKeys.includes(key)));
  return <><div className="obs-context-grid">{messageKeys.map((key) => <DetailScope id={key} key={key}><section>
    <h4>{FIELD_NAMES[key]}</h4><ContextMessages value={fields[key]} />
  </section></DetailScope>)}</div>{!!Object.keys(other).length && <DetailScope id="metadata"><ConfigFields value={other} /></DetailScope>}</>;
}

export function Disclosure({ title, children, stateKey }: { title: ReactNode; children: ReactNode; stateKey?: string }) {
  const [open, setOpen] = useDetailValue<boolean>("disclosure:" + (stateKey ?? String(title)), false);
  return <details className="obs-disclosure" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{title}</summary>{open && <DetailScope id={stateKey ?? String(title)}><div className="obs-disclosure-content">{children}</div></DetailScope>}
  </details>;
}

export function useInView() {
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

export function ObservationPayload({ instanceId, runId, reference, expired = false, captureState, title, active = true, bodyField }: {
  instanceId: string; runId: string; reference?: string; expired?: boolean; captureState?: string; title: string; active?: boolean;
  bodyField?: "input" | "output";
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
  const fields = content && typeof content === "object" && !Array.isArray(content) ? content as Record<string, unknown> : undefined;
  const selected = bodyField && fields && bodyField in fields ? fields[bodyField] : content;
  const primary = bodyField === "output" && selected && typeof selected === "object" && !Array.isArray(selected) &&
    (selected as Record<string, unknown>).channel === "not_traversed" ? { ...selected, channel: "未经过" } : selected;
  const extra = bodyField && fields && bodyField in fields ? Object.fromEntries(Object.entries(fields).filter(([key]) => key !== bodyField)) : {};
  return <section ref={ref} className="obs-payload" aria-label={title}><DetailScope id={"body:" + (reference ?? title)}>
    <div className="obs-pane-heading"><strong>{title}</strong><span>{bodyState(state)}</span></div>
    {state === "expired" ? <p className="obs-muted">详细正文已到期，结构化记录仍保留。</p> : query.error ?
      <Alert type="error" content={<span>详情读取失败：{query.error.message} <Button size="mini" onClick={() => void query.refetch()}>重试</Button></span>} /> :
      query.isFetching ? <Spin size={16} /> : payload != null ? <>
        <PayloadContent value={primary} />
        {!!Object.keys(extra).length && <DetailScope id="metadata"><ConfigFields value={extra} /></DetailScope>}
        <Disclosure title="原始记录"><TextPreview text={JSON.stringify(payload, null, 2)} /></Disclosure>
      </> : <p className="obs-muted">{reference && !visible ? "滚动到此处时加载" : reference ? "等待读取" : bodyState(state)}</p>}
  </DetailScope></section>;
}
