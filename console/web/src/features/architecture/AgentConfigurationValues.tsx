import { Disclosure, FIELD_NAMES, TextPreview } from "./ObservationContent";
import type { InspectionEntity } from "./workbenchModel";

const NAMES: Record<string, string> = {
  base_url: "服务地址", api_key: "API 凭据", timeout: "请求超时（秒）", model_slot: "默认模型来源",
  credential_env: "凭据环境变量", credential_configured: "已配置凭据",
  budget: "执行预算", selector: "工具选择范围", context_policy: "上下文策略", cache_policy: "缓存策略",
  tool_name: "委托工具名", workflow_tags: "Workflow 标签", cleanup_tools: "清理工具", unavailable_message: "不可用时说明",
  role_prompt_source: "角色提示词来源",
  model_env_prefix: "模型覆盖前缀", max_context_tokens: "上下文预算（Token）", sliding_window_turns: "保留对话轮数",
  max_tool_retries: "工具重试次数", tool_result_summary_max_tokens: "工具结果摘要预算（Token）",
  max_tool_iterations: "循环轮数软限制", hard_iteration_cap: "循环轮数硬限制", max_tool_calls: "工具调用硬限制",
  turn_timeout_seconds: "单轮软超时（秒）", hard_timeout_seconds: "单轮硬超时（秒）", stall_window_seconds: "无进展窗口（秒）",
  network_access: "命令网络访问", sandbox_mode: "命令沙箱策略", connected_apps: "连接应用", image_generation: "图像生成",
  web_search_mode: "原生 Web 搜索模式", presets: "已选择预设", custom_agents: "自定义 Agent", workflows: "已选择 Workflow",
  tools: "声明工具成员", dynamic: "按会话构造", steps: "执行步骤", optional_steps: "可选步骤", retry_map: "重试关系",
  max_retries: "重试上限", max_depth: "深度上限", input_schema: "输入定义", output_schema: "输出定义",
  include_tool_summary: "包含工具摘要", include_history: "包含历史对话", include_allowed_tools: "包含允许工具",
  allowed_task_fields: "允许任务字段", ttl_seconds: "缓存有效期（秒）", include_resource_hashes: "包含资源指纹",
  any: "任一规则满足即可", exclude_names: "排除工具名", names: "工具名", name_prefixes: "名称前缀",
  categories: "工具分类", category_prefixes: "分类前缀", owners: "工具归属", module_prefixes: "模块前缀", tags: "标签", mcp_risk: "MCP 风险分类",
};
const UNLIMITED = new Set(["hard_iteration_cap", "max_tool_calls", "turn_timeout_seconds", "hard_timeout_seconds"]);
const LONG_FIELDS = new Set(["input_schema", "output_schema", "parameters", "role_prompt"]);

function label(key: string, entity: InspectionEntity) {
  if (entity.id === "model-slot:code" && key === "enabled") return "允许模型切换命令";
  if (entity.id === "agent:main" && key === "model") return "实例默认模型";
  if (entity.id === "model-slot:code" && key === "command") return "Codex 命令模板";
  return NAMES[key] ?? FIELD_NAMES[key] ?? key;
}

export function observedConfiguration(entity: InspectionEntity): Record<string, unknown> | undefined {
  if (entity.runtime_stale || !entity.runtime) return undefined;
  if (entity.id !== "model-slot:code") return entity.runtime;
  return Object.fromEntries(Object.entries(entity.runtime).map(([key, value]) =>
    [key === "code_task_profile" ? key : key.replace(/^code_/, ""), value]));
}

function emptyValue(key: string, entity: InspectionEntity) {
  if (UNLIMITED.has(key)) return "未设置限制";
  if (key === "model_env_prefix") return "继承基础模型";
  if (key === "sandbox_mode") return "由宿主执行范围决定";
  if (key === "credential_env" || key === "credential_configured") return "无需凭据";
  if (key === "reasoning_effort" && entity.id === "agent:main") return "当前 Backend 不适用";
  return "未配置";
}

export default function AgentConfigurationValues({ entity, value = entity.config, path = "", observed = observedConfiguration(entity) }: {
  entity: InspectionEntity; value?: unknown; path?: string; observed?: unknown;
}) {
  const key = path.split(".").slice(-1)[0] ?? "";
  if (value == null || value === "") return <span className="obs-muted">{emptyValue(key, entity)}</span>;
  if (Array.isArray(value)) return value.length ? <ul className="config-value-list">{value.map((item, index) =>
    <li key={index}><AgentConfigurationValues entity={entity} value={item} path={`${path}.${index}`} observed={null} /></li>)}</ul> :
    <span className="obs-muted">{key === "presets" ? "未选择预设" : key === "workflows" ? "未选择 Workflow" : key === "custom_agents" ? "未配置自定义 Agent" : "空列表"}</span>;
  if (typeof value !== "object") return <span className="config-value">{typeof value === "boolean" ? value ? "是" : "否" : String(value)}</span>;
  const entries = Object.entries(value);
  if (!entries.length) return <span className="obs-muted">无额外配置参数</span>;
  return <dl className="config-values config-annotated-values">{entries.map(([field, item]) => {
    const fieldPath = path ? `${path}.${field}` : field;
    const sources = entity.field_sources ?? {};
    const source = sources[fieldPath] ?? sources[path] ?? (path ? undefined : "实例配置投影");
    const live = observed && typeof observed === "object" ? observed as Record<string, unknown> : undefined;
    const differing = (item == null || typeof item !== "object" || Array.isArray(item)) && live &&
      Object.prototype.hasOwnProperty.call(live, field) && JSON.stringify(live[field]) !== JSON.stringify(item);
    return <div key={field}>
      <dt title={fieldPath}>{label(field, entity)}<small className="config-field-key">{field}</small></dt>
      <dd>{LONG_FIELDS.has(field) || typeof item === "string" && item.length > 500 ?
        <Disclosure title={`查看${label(field, entity)}`}><TextPreview text={typeof item === "string" ? item : JSON.stringify(item, null, 2)} /></Disclosure> :
        <AgentConfigurationValues entity={entity} value={item} path={fieldPath} observed={live?.[field] ?? null} />}
        {source && <small className="config-field-source">{source}</small>}
        {differing && <div className="config-runtime-difference"><span>服务当前值</span>
          <AgentConfigurationValues entity={entity} value={live[field]} path={fieldPath} observed={null} /></div>}
      </dd>
    </div>;
  })}</dl>;
}
