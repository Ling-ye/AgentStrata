import type { BotToolConfig } from "../../types";
import type { Configuration, InspectionEntity } from "./workbenchModel";

// Presentation ownership only. Original layer/IDs also identify historical evidence.
export const CONFIGURATION_LAYERS = [
  { id: "channel", name: "Channel · 渠道", description: "平台连接与消息适配", groups: [["channel", "渠道连接"], ["platform", "平台适配"]] },
  { id: "gateway", name: "Gateway · 网关", description: "协议、身份与准入", groups: [["gateway", "网关与协议"], ["access", "身份与准入"]] },
  { id: "application", name: "Application · 应用", description: "会话、资源与上下文准备", groups: [["workspace", "会话与工作区"], ["memory", "记忆与知识"], ["resources", "资料与项目"], ["features", "输入处理"]] },
  { id: "agent", name: "Agent · 智能体", description: "实例默认模型、能力用途与完整参数", groups: [["model", "主 Agent 与模型"], ["prompts", "提示词"], ["search", "搜索与研究"], ["delegation", "子 Agent 与委托"], ["packs", "工具包"], ["mcp", "MCP"], ["tools", "具体工具"]] },
] as const;
export type ConfigurationLayer = typeof CONFIGURATION_LAYERS[number]["id"];
export type ConfigurationGroup = typeof CONFIGURATION_LAYERS[number]["groups"][number][0] | "instance";
export type DisplayEntity = InspectionEntity & { group: ConfigurationGroup; displayLayer: ConfigurationLayer | null; viewKey: string; applicable: boolean };

export function layerForGroup(group: ConfigurationGroup): ConfigurationLayer | null {
  return CONFIGURATION_LAYERS.find((layer) => layer.groups.some(([id]) => id === group))?.id ?? null;
}
export function configurationGroup(entity: Pick<InspectionEntity, "id" | "layer">): ConfigurationGroup {
  const { id, layer } = entity;
  if (id.startsWith("mcp:")) return "mcp";
  if (/^(subagent:|workflow:)/.test(id) || ["agent:delegation", "agent:code-task"].includes(id)) return "delegation";
  if (/^(skill:|codebase:)/.test(id) || ["context:playbooks", "context:codebases", "context:codebase-details", "context:dev"].includes(id)) return "resources";
  if (id.startsWith("search:") || ["agent:unified-search", "agent:search-budget", "agent:codex-web-search"].includes(id)) return "search";
  if (id.startsWith("pack:")) return "packs";
  if (id.startsWith("feature:")) return "features";
  if (/^(tool:|tools:)/.test(id)) return "tools";
  if (id.startsWith("rag:") || ["context:rag", "context:rag-details", "context:wiki"].includes(id)) return "memory";
  if (id === "prompts:instance") return "prompts";
  if (id.startsWith("agent:") || id.startsWith("model-slot:")) return "model";
  if (id === "platform:instance") return "platform";
  if (id.startsWith("channel:")) return "channel";
  if (id === "gateway:instance") return "gateway";
  if (id === "policy:instance") return "access";
  if (id === "workspace:instance") return "workspace";
  return ({ control: "instance", gateway: "gateway", channel: "channel", authorization: "access",
    application: "resources", agent: "model", capability: "tools", botspec: "instance", contracts: "instance" } as Record<string, ConfigurationGroup>)[layer] ?? "instance";
}

export function configurationLink(instanceId: string, id: string, group = configurationGroup({ id, layer: "" })) {
  const params = new URLSearchParams({ instance: instanceId, tab: "configuration", section: group, entity: id });
  const layer = layerForGroup(group);
  if (layer) params.set("layer", layer);
  return "#bots?" + params;
}

export function configurationSelection(entities: DisplayEntity[], params: URLSearchParams, savedLayer?: unknown) {
  const id = params.get("entity");
  const selected = entities.find((entity) => entity.id === id && entity.group === params.get("section")) ?? entities.find((entity) => entity.id === id);
  const requested = params.get("layer") || savedLayer;
  const layer = selected?.displayLayer ?? CONFIGURATION_LAYERS.find((item) => item.id === requested)?.id ?? "channel";
  return { layer, selected, missing: !!id && !selected };
}

export function configurationSearch(entities: DisplayEntity[], search: string) {
  const query = search.trim().toLocaleLowerCase();
  return query ? entities.filter((entity) => [entity.name, entity.id, entity.group, entity.usage, entity.applicability, JSON.stringify(entity.field_sources), JSON.stringify(entity.config), JSON.stringify(entity.environment)]
    .join(" ").toLocaleLowerCase().includes(query)) : [];
}

export function configurationSummary(entity: DisplayEntity): string {
  if (!entity.applicable) return "当前实例不适用";
  const config = entity.config ?? {};
  const values = entity.environment ?? {};
  if (entity.id === "agent:main") return `${config.runtime_id ?? "未记录"} · 实例默认模型 ${config.model ?? "未记录"}${config.reasoning_effort ? " / " + config.reasoning_effort : ""}`;
  if (entity.id === "agent:code-task") return `${config.code_task_profile || "未选择配置档"} · ${config.model ?? "未配置"}${config.reasoning_effort ? " / " + config.reasoning_effort : ""}`;
  if (entity.id.startsWith("pack:")) return config.description ? String(config.description) : config.dynamic ? "按会话构造工具" : Array.isArray(config.tools) ? `${config.tools.length} 个声明工具` : "工具包";
  const fields = ["runtime_id", "model", "provider", "type", "transport", "command", "endpoint", "endpoint_env", "root_env", "namespace", "ref", "registry", "manifest", "identity"];
  const summary = fields.flatMap((field) => {
    const raw = config[field];
    const value = field.endsWith("_env") && typeof raw === "string" ? values[raw] : raw;
    return typeof value === "string" && value ? [value] : [];
  });
  return summary.slice(0, 2).join(" · ") || (entity.configured === false ? "未启用" : Object.keys(config).length ? "查看配置字段" : "未配置参数");
}

export function configurationRelated(entities: DisplayEntity[], entity: DisplayEntity) {
  const resources: Record<string, string[]> = {
    "context:wiki": ["pack:wiki.knowledge"],
    "context:playbooks": ["pack:playbooks.reader"], "context:codebases": ["pack:codebase.read"],
    "context:dev": ["pack:dev.files", "pack:dev.shell", "pack:dev.code_tasks"],
    "workspace:instance": ["pack:workspace.read_write"],
  };
  const source = entity.id.startsWith("skill:") ? "context:playbooks" : entity.id.startsWith("codebase:") ? "context:codebases" : entity.id;
  const ids = new Set([...(entity.refs ?? []), ...(resources[source] ?? []),
    ...Object.entries(resources).filter(([, packs]) => packs.includes(entity.id)).map(([id]) => id)]);
  return entities.filter((item) => item.id !== entity.id && (ids.has(item.id) || item.refs?.includes(entity.id)));
}

export function configurationViews(configuration: Configuration, draft?: BotToolConfig | null): DisplayEntity[] {
  const result: DisplayEntity[] = [];
  const push = (entity: InspectionEntity, group: ConfigurationGroup, config = entity.config, name = entity.name) => {
    const values = entity.effective_environment ?? entity.environment;
    const refs = JSON.stringify(config) ?? "";
    const environment = entity.id.startsWith("model-slot:") ? values : values && Object.fromEntries(Object.entries(values).filter(([key]) =>
      refs.includes(key) || Object.entries(config ?? {}).some(([field, prefix]) => field.endsWith("env_prefix") && typeof prefix === "string" && key.startsWith(prefix + "_"))));
    result.push({ ...entity, source_config: entity.source_config ?? entity.config,
      source_environment: Object.fromEntries(Object.entries(entity.source_environment ?? {}).filter(([key]) => key in (environment ?? {}))),
      config, name, environment, effective_environment: environment, group, displayLayer: layerForGroup(group),
      applicable: !(entity.id === "gateway:instance" && !entity.configured) && !(entity.id === "platform:instance" && !config?.type && !config?.adapter),
      viewKey: `${group}:${entity.id}` });
  };
  for (const original of configuration.entities) {
    const entity = { ...original, source_config: original.config, source_environment: original.environment, config: { ...(original.effective_config ?? original.config) } };
    const config = entity.config ?? {};
    if (entity.id.startsWith("pack:")) delete config.hidden_tools;
    // Editable memberships come from the shared draft, including disabled MCP entries.
    if (draft && entity.configured !== null) {
      if (entity.id.startsWith("pack:") && !draft.tools.packs.includes(entity.id.slice(5))) continue;
      if (entity.id.startsWith("feature:") && !draft.tools.features.includes(entity.id.slice(8))) continue;
      if (entity.id.startsWith("subagent:") && entity.membership !== "custom" && !draft.agents.presets.includes(entity.id.slice(9))) continue;
      if (entity.id.startsWith("workflow:") && !draft.agents.workflows.includes(entity.id.slice(9))) continue;
      if (entity.id.startsWith("tool:")) entity.configured = !draft.tools.hide.includes(entity.id.slice(5));
      if (entity.id.startsWith("mcp:")) {
        const server = draft.tools.mcp.servers.find((item) => item.ref === (config.catalog_ref ?? config.ref ?? entity.id.slice(4)));
        if (!server && (config.catalog_ref || config.ref)) continue;
        if (server) { config.enabled = server.enabled; entity.configured = server.enabled; }
      }
    }
    push(entity, configurationGroup(entity));
  }
  if (draft) {
    const add = (id: string, group: ConfigurationGroup, config: Record<string, unknown> = {}) => {
      if (!result.some((item) => item.id === id || id.startsWith("mcp:") && (item.config?.catalog_ref ?? item.config?.ref) === id.slice(4)))
        push({ id, layer: "capability", name: id.slice(id.indexOf(":") + 1), configured: config.enabled !== false, loaded: null,
          connected: null, available: null, config }, group);
    };
    draft.tools.packs.forEach((id) => add(`pack:${id}`, "packs"));
    draft.tools.features.forEach((id) => add(`feature:${id}`, "features"));
    draft.tools.mcp.servers.forEach((item) => add(`mcp:${item.ref}`, "mcp", { ref: item.ref, enabled: item.enabled }));
    draft.agents.presets.forEach((id) => add(`subagent:${id}`, "delegation"));
    draft.agents.workflows.forEach((id) => add(`workflow:${id}`, "delegation"));
  }
  // Keep main summary first even when normalized entities were appended by inspection.
  return result.sort((a, b) => Number(b.id === "agent:main") - Number(a.id === "agent:main"));
}
