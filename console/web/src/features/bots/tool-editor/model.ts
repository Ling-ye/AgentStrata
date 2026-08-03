import type { BotInventory, CatalogItem, Task } from "../../../types";

export const SURFACE_TEXT = {
  tools: "工具",
  toolsHelp: "本地能力、MCP 服务和隐藏工具。",
  localCapabilities: "本地能力",
  addCapability: "添加能力",
  toolTag: "工具",
  runtimeFeatures: "运行特性",
  addRuntimeFeature: "添加运行特性",
  runtimeFeatureHelp: "运行特性用于启用文件上传、私人空间等能力，不会直接暴露为模型可调用工具。",
  mcpServices: "MCP 服务",
  addMcp: "添加 MCP",
  disabled: "已禁用",
  hiddenTools: "隐藏工具",
  prompts: "提示词",
  promptsHelp: "Persona、拒答、安全和角色提示词配置。",
  promptsLoading: "提示词配置仍在加载。",
  agentsHelp: "内部子代理和确定性 workflow。",
  subagents: "子代理",
  addSubagent: "添加子代理",
  addWorkflow: "添加 Workflow",
  context: "上下文",
  contextHelp: "记忆、RAG、代码仓库和 playbooks 来源。",
  contextLoading: "上下文配置仍在加载。",
  empty: "暂无",
};

export interface BotToolEditorProps {
  instanceId: string;
  isDeployed?: boolean;
  inventory?: BotInventory;
  onApplyTask?: (task: Task, onSuccess: () => void) => void;
}

export type PickerTarget = "capability" | "mcp" | "subagent" | "workflow" | null;
export type SurfaceKey = "tools" | "prompts" | "agents" | "context";

export function groupCatalog(items: CatalogItem[] | undefined): Record<string, CatalogItem[]> {
  const grouped: Record<string, CatalogItem[]> = { tool_pack: [], tool_feature: [], mcp: [], subagent: [], workflow: [] };
  for (const item of items ?? []) {
    if (grouped[item.kind]) grouped[item.kind].push(item);
  }
  return grouped;
}

export function indexBy<T>(items: T[] | undefined, key: (item: T) => string): Map<string, T> {
  return new Map((items ?? []).map((item) => [key(item), item]));
}
