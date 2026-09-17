import type { BotToolConfig } from "../../types";
import type { Configuration, InspectionEntity } from "./workbenchModel";

// Presentation ownership only. Original layer/IDs also identify historical evidence.
export const CONFIGURATION_LAYERS = [
  { id: "channel", name: "Channel · 渠道", description: "平台连接与消息适配", groups: [["channel", "渠道连接"], ["platform", "平台适配"]] },
  { id: "gateway", name: "Gateway · 网关", description: "协议、身份与准入", groups: [["gateway", "网关与协议"], ["access", "身份与准入"]] },
  { id: "application", name: "Application · 应用", description: "会话、资源与上下文准备", groups: [["workspace", "会话与工作区"], ["memory", "记忆与知识"], ["resources", "资料与项目"], ["features", "输入处理"]] },
  { id: "agent", name: "Agent · 智能体", description: "模型、工具与委托执行", groups: [["model", "模型与提示词"], ["packs", "工具包"], ["mcp", "MCP"], ["search", "搜索"], ["delegation", "子 Agent 与委托"], ["tools", "具体工具"]] },
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
  if (/^(subagent:|workflow:)/.test(id)) return "delegation";
  if (/^(skill:|codebase:)/.test(id) || ["context:playbooks", "context:codebases", "context:codebase-details", "context:dev"].includes(id)) return "resources";
  if (id.startsWith("search:")) return "search";
  if (id.startsWith("pack:")) return "packs";
  if (id.startsWith("feature:")) return "features";
  if (/^(tool:|tools:)/.test(id)) return "tools";
  if (id.startsWith("rag:") || ["context:rag", "context:rag-details", "context:memory_store", "context:wiki"].includes(id)) return "memory";
  if (id === "prompts:instance" || id === "agent:main" || id.startsWith("model-slot:")) return "model";
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
  return query ? entities.filter((entity) => [entity.name, entity.id, entity.group, JSON.stringify(entity.config), JSON.stringify(entity.environment)]
    .join(" ").toLocaleLowerCase().includes(query)) : [];
}

export function configurationSummary(entity: DisplayEntity): string {
  if (!entity.applicable) return "当前实例不适用";
  const config = entity.config ?? {};
  const values = entity.environment ?? {};
  const fields = ["backend", "model", "provider", "type", "transport", "command", "endpoint", "endpoint_env", "root_env", "namespace", "ref", "registry", "manifest", "identity"];
  const summary = fields.flatMap((field) => {
    const raw = config[field];
    const value = field.endsWith("_env") && typeof raw === "string" ? values[raw] : raw;
    return typeof value === "string" && value ? [value] : [];
  });
  if (!summary.length) {
    const model = Object.entries(values).find(([key, value]) => key.endsWith("_MODEL") && typeof value === "string" && value);
    if (model) summary.push(String(model[1]));
  }
  return summary.slice(0, 2).join(" · ") || (entity.configured === false ? "未启用" : Object.keys(config).length ? "查看配置字段" : "未配置参数");
}

export function configurationRelated(entities: DisplayEntity[], entity: DisplayEntity) {
  const resources: Record<string, string[]> = {
    "context:memory_store": ["pack:memory.chat"], "context:wiki": ["pack:wiki.knowledge"],
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
    const environment = values && Object.fromEntries(Object.entries(values).filter(([key]) =>
      entity.id === "model-slot:code" || refs.includes(key) ||
      Object.entries(config ?? {}).some(([field, prefix]) => field.endsWith("env_prefix") && typeof prefix === "string" && key.startsWith(prefix + "_") && !/_(CODE|ROUTER|RESEARCH)_/.test(key))));
    result.push({ ...entity, source_config: Object.fromEntries(Object.entries(entity.source_config ?? entity.config ?? {}).filter(([key]) => key in (config ?? {}))),
      source_environment: Object.fromEntries(Object.entries(entity.source_environment ?? {}).filter(([key]) => key in (environment ?? {}))),
      config, name, environment, effective_environment: environment, group, displayLayer: layerForGroup(group),
      applicable: !(entity.id === "gateway:instance" && !entity.configured) && !(entity.id === "platform:instance" && !config?.type && !config?.adapter),
      viewKey: `${group}:${entity.id}` });
  };
  for (const original of configuration.entities) {
    const entity = { ...original, source_config: original.config, source_environment: original.environment, config: { ...(original.effective_config ?? original.config) } };
    const config = entity.config ?? {};
    if (entity.id === "agent:main") {
      const delegationKeys = new Set(["include", "presets", "defaults", "agents", "overrides", "custom", "workflows", "max_workflow_depth"]);
      const searchKeys = new Set(["search_budget", "search", "research_enabled", "research_budget"]);
      push(entity, "model", Object.fromEntries(Object.entries(config).filter(([key]) => !delegationKeys.has(key) && !searchKeys.has(key) && key !== "search_providers")));
      // The source ID stays unchanged; each field has one presentation owner.
      const delegation = Object.fromEntries(Object.entries(config).filter(([key]) => delegationKeys.has(key) && !["overrides", "presets", "include", "workflows"].includes(key)));
      const selected = new Set(configuration.entities.filter((item) => item.id.startsWith("subagent:")).map((item) => item.id.slice(9)));
      const unselected = Object.fromEntries(Object.entries((config.overrides ?? {}) as Record<string, unknown>).filter(([id]) => !selected.has(id)));
      if (Object.keys(unselected).length) delegation.overrides = unselected;
      push(entity, "delegation", delegation, "委托与预算");
      const search = Object.fromEntries(Object.entries(config).filter(([key]) => searchKeys.has(key)));
      if (search.search && typeof search.search === "object") search.search = Object.fromEntries(Object.entries(search.search).filter(([key]) => key !== "providers"));
      if (Object.keys(search).length) push(entity, "search", search, "搜索预算");
      continue;
    }
    if (entity.id.startsWith("pack:")) delete config.hidden_tools;
    // Editable memberships come from the shared draft, including disabled MCP entries.
    if (draft && entity.configured !== null) {
      if (entity.id.startsWith("pack:") && !draft.tools.packs.includes(entity.id.slice(5))) continue;
      if (entity.id.startsWith("feature:") && !draft.tools.features.includes(entity.id.slice(8))) continue;
      if (entity.id.startsWith("subagent:") && !draft.agents.presets.includes(entity.id.slice(9))) continue;
      if (entity.id.startsWith("workflow:") && !draft.agents.workflows.includes(entity.id.slice(9))) continue;
      if (entity.id.startsWith("mcp:")) {
        const server = draft.tools.mcp.servers.find((item) => item.ref === (config.catalog_ref ?? config.ref ?? entity.id.slice(4)));
        if (!server && (config.catalog_ref || config.ref)) continue;
        if (server) { entity.config = { ...config, enabled: server.enabled }; entity.configured = server.enabled; }
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
  return result;
}
