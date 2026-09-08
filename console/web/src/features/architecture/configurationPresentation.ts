import type { BotToolConfig } from "../../types";
import type { Configuration, InspectionEntity } from "./workbenchModel";

export type ConfigurationTab = "configuration" | "capabilities";
export const CONFIGURATION_GROUPS = [
  ["deployment", "部署与服务"], ["gateway", "Gateway 与协议"], ["channel", "Channel 与平台"],
  ["access", "身份与权限"], ["workspace", "会话与工作区"], ["model", "Agent 与模型"],
  ["delegation", "子 Agent 与委托"], ["memory", "记忆与 RAG"], ["mcp", "MCP"], ["instance", "提示词与实例信息"],
] as const;
export const CAPABILITY_GROUPS = [["plugins", "独立功能与插件"], ["skills", "Skills"], ["search", "搜索 Provider"],
  ["packs", "工具包"], ["tools", "具体工具"], ["features", "运行特性"]] as const;
export type DisplayEntity = InspectionEntity & { group: string; viewKey: string };

export function configurationTabForEntity(id: string): ConfigurationTab {
  return /^(tool:|pack:|feature:|skill:|search:|codebase:|tools:)/.test(id) ||
    ["context:wiki", "context:playbooks", "context:dev", "context:codebases", "context:codebase-details"].includes(id)
    ? "capabilities" : "configuration";
}
function groupFor(entity: InspectionEntity): string {
  const { id, layer } = entity;
  if (id.startsWith("mcp:")) return "mcp";
  if (/^(subagent:|workflow:)/.test(id)) return "delegation";
  if (/^(skill:)/.test(id) || id === "context:playbooks") return "skills";
  if (id.startsWith("search:")) return "search";
  if (id.startsWith("pack:")) return "packs";
  if (id.startsWith("feature:")) return "features";
  if (id.startsWith("tool:")) return "tools";
  if (configurationTabForEntity(id) === "capabilities") return "plugins";
  if (id.startsWith("rag:") || ["context:rag", "context:rag-details", "context:memory_store"].includes(id)) return "memory";
  if (id === "prompts:instance") return "instance";
  return ({ control: "deployment", gateway: "gateway", channel: "channel", authorization: "access",
    application: "workspace", agent: "model", botspec: "instance", contracts: "instance" } as Record<string, string>)[layer] ?? "plugins";
}

export function configurationViews(configuration: Configuration, draft?: BotToolConfig | null): DisplayEntity[] {
  const result: DisplayEntity[] = [];
  const push = (entity: InspectionEntity, group: string, config = entity.config, name = entity.name) => {
    const values = entity.effective_environment ?? entity.environment;
    const refs = JSON.stringify(config);
    const environment = values && Object.fromEntries(Object.entries(values).filter(([key]) =>
      entity.id === "model-slot:code" || refs.includes(key) ||
      Object.entries(config ?? {}).some(([field, prefix]) => field.endsWith("env_prefix") && typeof prefix === "string" && key.startsWith(prefix + "_") && !/_(CODE|ROUTER|RESEARCH)_/.test(key))));
    result.push({ ...entity, source_config: Object.fromEntries(Object.entries(entity.source_config ?? entity.config ?? {}).filter(([key]) => key in (config ?? {}))),
      source_environment: Object.fromEntries(Object.entries(entity.source_environment ?? {}).filter(([key]) => key in (environment ?? {}))),
      config, name, environment, effective_environment: environment, group, viewKey: `${group}:${entity.id}` });
  };
  for (const original of configuration.entities) {
    const entity = { ...original, source_config: original.config, source_environment: original.environment, config: { ...(original.effective_config ?? original.config) } };
    const config = entity.config ?? {};
    if (entity.id === "agent:main") {
      const delegationKeys = new Set(["include", "presets", "defaults", "agents", "overrides", "custom", "workflows", "max_workflow_depth", "research_enabled", "research_budget"]);
      const searchKeys = new Set(["search_budget", "search"]);
      push(entity, "model", Object.fromEntries(Object.entries(config).filter(([key]) => !delegationKeys.has(key) && !searchKeys.has(key) && key !== "search_providers")));
      // The source ID stays unchanged; each field has one presentation owner.
      const delegation = Object.fromEntries(Object.entries(config).filter(([key]) => delegationKeys.has(key) && !["overrides", "presets", "include", "workflows"].includes(key)));
      const selected = new Set(configuration.entities.filter((item) => item.id.startsWith("subagent:")).map((item) => item.id.slice(9)));
      const unselected = Object.fromEntries(Object.entries((config.overrides ?? {}) as Record<string, unknown>).filter(([id]) => !selected.has(id)));
      if (Object.keys(unselected).length) delegation.overrides = unselected;
      push(entity, "delegation", delegation, "委托与预算");
      const search = Object.fromEntries(Object.entries(config).filter(([key]) => searchKeys.has(key)));
      if (search.search && typeof search.search === "object") search.search = Object.fromEntries(Object.entries(search.search).filter(([key]) => key !== "providers"));
      if (configuration.entities.some((item) => item.id.startsWith("search:"))) push(entity, "search", search, "搜索预算");
      continue;
    }
    if (entity.id === "context:wiki" && !config.enabled && !entity.loaded) continue;
    if (entity.id === "context:playbooks" && !config.manifest && !entity.loaded) continue;
    if (entity.id === "context:codebases" && !config.registry && !entity.loaded) continue;
    if (entity.id === "context:dev" && !configuration.entities.some((item) => /^(pack:dev\.|feature:dev\.)/.test(item.id))) continue;
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
    push(entity, groupFor(entity));
  }
  if (draft) {
    const add = (id: string, group: string, config: Record<string, unknown> = {}) => {
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
