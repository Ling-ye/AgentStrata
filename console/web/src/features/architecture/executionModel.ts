import type { GatewayObservation } from "./model";
import { agentProcess } from "./agentProcessModel";
import { RUNTIME_LAYERS, RUNTIME_OPERATIONS, stepState, type FlowItem, type RuntimeLayer } from "./workbenchModel";

export interface ExecutionNode {
  id: string;
  item: FlowItem;
  title: string;
  kind: string;
  parentId?: string;
}
export interface ExecutionRelation {
  id: string;
  kind: "parent" | "call" | "input";
  source: string;
  target: string;
  label: string;
  field: string;
  value: string;
  event: GatewayObservation;
  messageIndex?: number;
}
export interface RelationGap { nodeId: string; message: string }
export interface ExecutionModel {
  nodes: ExecutionNode[];
  byId: Map<string, ExecutionNode>;
  relations: ExecutionRelation[];
  gaps: RelationGap[];
  stages: Array<{ stage?: ExecutionNode; nodes: ExecutionNode[] }>;
}
export interface RuntimeStageProjection {
  stages: Array<{ stage: ExecutionNode; nodes: ExecutionNode[] }>;
  unbound: Array<{ key: string; layer?: RuntimeLayer; missingStage: boolean; nodes: ExecutionNode[] }>;
}
export interface AgentExecutionScope {
  key: string;
  root?: ExecutionNode;
  nodes: ExecutionNode[];
  ids: ReadonlySet<string>;
  relations: ExecutionRelation[];
  gaps: RelationGap[];
}

export const spanKey = (trace?: string, span?: string) => trace && span ? JSON.stringify([trace, span]) : undefined;
export const PROCESS_LABELS: Record<string, string> = { stage: "阶段", agent: "主 Agent", model_call: "模型调用",
  backend_execution: "后端执行", tool: "工具", subagent: "子 Agent", workflow: "工作流", message: "消息",
  reasoning: "公开摘要", command: "命令", file_change: "文件变更", mcp_tool: "MCP 调用", web_search: "搜索",
  plan: "计划", tool_catalog: "工具接入", resources: "输入资源", context: "上下文", error: "异常", event: "事件" };
export function executionTitle(item: FlowItem) {
  if (item.kind === "stage") return RUNTIME_OPERATIONS[item.operation ?? ""] ?? item.operation ?? "运行阶段";
  const process = agentProcess(item.step);
  if (!process.title && process.kind === "agent") return "主 Agent 执行过程";
  return process.title || ({ ContextSnapshotPrepared: "上下文快照", InputResourcesDispatched: "输入资源",
    AgentProcessCaptureFailed: "执行过程采集失败", TurnError: "任务异常", session_capabilities: "本次可用能力" }[item.step.event.kind]
    ?? item.step.event.kind);
}
function data(node: ExecutionNode) { return { ...node.item.step.start?.data, ...node.item.step.event.data }; }

/** One projection for all views. IDs and edges come only from recorded references. */
export function buildExecutionModel(flow: FlowItem[]): ExecutionModel {
  const nodes = flow.map((item): ExecutionNode => ({ id: item.step.key, item, title: executionTitle(item),
    kind: item.kind === "stage" ? "stage" : agentProcess(item.step).kind || "event" }));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const relations: ExecutionRelation[] = [], gaps: RelationGap[] = [];
  const link = (kind: ExecutionRelation["kind"], source: string, target: string, field: string,
    value: string, event: GatewayObservation, messageIndex?: number) => {
    relations.push({ id: JSON.stringify([kind, source, target, messageIndex]), kind, source, target,
      label: { parent: "父子归属", call: "触发调用", input: "进入上下文" }[kind], field, value, event, messageIndex });
  };
  for (const node of nodes) {
    const step = node.item.step;
    const parent = spanKey(step.event.trace_id, step.event.parent_span_id ?? step.start?.parent_span_id);
    if (parent && byId.has(parent) && !step.missingParent) {
      node.parentId = parent;
      link("parent", parent, node.id, "parent_span_id", step.event.parent_span_id ?? step.start!.parent_span_id!, step.start ?? step.event);
    } else if (parent) gaps.push({ nodeId: node.id, message: "父调用未取得或关系异常" });
    const model = data(node).model_span_id;
    if (typeof model === "string" && model) {
      const caller = spanKey(step.event.trace_id, model)!;
      if (byId.has(caller) && caller !== node.id) link("call", caller, node.id, "model_span_id", model, step.start ?? step.event);
      else gaps.push({ nodeId: node.id, message: "触发本步骤的模型调用尚未取得" });
    }
  }
  // The nearest Agent boundary scopes call IDs; same-name/same-ID calls in other delegates cannot match.
  const scope = (node: ExecutionNode) => {
    let current = node;
    const seen = new Set<string>();
    while (!seen.has(current.id)) {
      seen.add(current.id);
      if (["subagent", "workflow", "backend_execution"].includes(current.kind) || current.item.operation === "agent.execute") return current.id;
      if (!current.parentId) return spanKey(current.item.step.event.trace_id,
        current.item.step.event.parent_span_id ?? current.item.step.start?.parent_span_id) ?? current.id;
      const parent = byId.get(current.parentId);
      if (!parent) return current.parentId;
      current = parent;
    }
    return node.id;
  };
  const tools = new Map<string, ExecutionNode[]>();
  for (const node of nodes) {
    const id = data(node).tool_call_id;
    if (node.kind !== "tool" || !node.item.step.finish || typeof id !== "string" || !id) continue;
    const key = JSON.stringify([node.item.step.event.trace_id, scope(node), id]);
    tools.set(key, [...(tools.get(key) ?? []), node]);
  }
  for (const node of nodes) {
    if (node.kind !== "model_call" && !agentProcess(node.item.step).modelCall) continue;
    for (const context of node.item.step.contexts) {
      const refs = context.data?.input_tool_refs;
      if (!Array.isArray(refs)) continue;
      for (const ref of refs) {
        if (!ref || typeof ref !== "object" || typeof ref.tool_call_id !== "string" ||
          !Number.isInteger(ref.message_index) || ref.message_index < 0) continue;
        const candidates = tools.get(JSON.stringify([node.item.step.event.trace_id, scope(node), ref.tool_call_id])) ?? [];
        if (candidates.length === 1 && candidates[0].id !== node.id) {
          link("input", candidates[0].id, node.id, "input_tool_refs", ref.tool_call_id, context, ref.message_index);
        } else gaps.push({ nodeId: node.id, message: `输入消息 ${ref.message_index + 1} 的工具来源${candidates.length ? "存在歧义" : "尚未取得"}` });
      }
    }
  }
  const stages: ExecutionModel["stages"] = [];
  const groups = new Map<string, ExecutionModel["stages"][number]>();
  for (const node of nodes) {
    if (node.item.kind === "stage") {
      const group = { stage: node, nodes: [] as ExecutionNode[] };
      groups.set(node.id, group); stages.push(group);
    } else {
      const group = node.item.stageKey && groups.get(node.item.stageKey);
      if (group) group.nodes.push(node);
      else stages.push({ nodes: [node] });
    }
  }
  return { nodes, byId, relations, gaps, stages };
}

/** Separate real runtime stages from recorded calls whose owning stage is unavailable. */
export function executionStageProjection(model: ExecutionModel): RuntimeStageProjection {
  const stages: RuntimeStageProjection["stages"] = [];
  const unbound: RuntimeStageProjection["unbound"] = [];
  const unboundByKey = new Map<string, RuntimeStageProjection["unbound"][number]>();
  for (const group of model.stages) {
    if (group.stage) {
      stages.push({ stage: group.stage, nodes: group.nodes });
      continue;
    }
    for (const node of group.nodes) {
      const missingStage = !!node.item.stageKey;
      const key = missingStage ? `missing:${node.item.stageKey}` : `unbound:${node.item.layer ?? "unknown"}`;
      let projected = unboundByKey.get(key);
      if (!projected) {
        projected = { key, layer: node.item.layer, missingStage, nodes: [] };
        unboundByKey.set(key, projected);
        unbound.push(projected);
      }
      projected.nodes.push(node);
    }
  }
  return { stages, unbound };
}

/** Resolve the selected Agent stage (or unbound Agent records) without inventing a stage root. */
export function agentExecutionScope(model: ExecutionModel, selectedId: string): AgentExecutionScope | undefined {
  const stage = model.stages.find((group) => group.stage?.id === selectedId || group.nodes.some((node) => node.id === selectedId));
  let key: string, root: ExecutionNode | undefined, nodes: ExecutionNode[] | undefined;
  if (stage?.stage?.item.layer === "agent") {
    key = stage.stage.id;
    root = stage.stage;
    nodes = [stage.stage, ...stage.nodes];
  } else {
    const unbound = executionStageProjection(model).unbound.find((group) =>
      group.layer === "agent" && group.nodes.some((node) => node.id === selectedId));
    if (!unbound) return undefined;
    key = unbound.key;
    nodes = unbound.nodes;
  }
  const ids = new Set(nodes.map((node) => node.id));
  return { key, root, nodes, ids,
    relations: model.relations.filter((relation) => ids.has(relation.source) && ids.has(relation.target)),
    gaps: model.gaps.filter((gap) => ids.has(gap.nodeId)) };
}

export function layerSummaries(model: ExecutionModel, terminal: boolean, hasMore: boolean) {
  return (Object.keys(RUNTIME_LAYERS) as RuntimeLayer[]).map((layer) => {
    const nodes = model.nodes.filter((node) => node.item.layer === layer);
    const states = nodes.map((node) => stepState(node.item.step, terminal, hasMore));
    const failed = states.filter((state) => ["failed", "error", "cancelled", "aborted"].includes(state.status)).length;
    const running = states.filter((state) => ["running", "pending", "submitting"].includes(state.status)).length;
    const notTraversed = layer === "channel" && !nodes.length && model.nodes.some((node) =>
      node.item.operation === "gateway.accept" && data(node).entrypoint === "client");
    return { layer, nodes: nodes.length, stages: nodes.filter((node) => node.item.kind === "stage").length,
      failed, running, label: failed ? `${failed} 项异常` : running ? `${running} 项进行中` :
        !nodes.length ? notTraversed ? "未经过" : hasMore ? "尚未取得" : "未记录" :
        states.some((state) => state.incomplete || ["unknown", "delivery_unknown", "truncated", "incomplete"].includes(state.status)) ? "记录不完整 / 状态未知" : "已记录" };
  });
}

export function visibleGraph(model: ExecutionModel, options: {
  collapsed: ReadonlySet<string>; focus?: string; search?: string; errorsOnly?: boolean; terminal: boolean; hasMore: boolean;
  scope?: ReadonlySet<string>;
}) {
  const agentNodes = model.nodes.filter((node) => options.scope ? options.scope.has(node.id) : node.item.layer === "agent");
  const ids = new Set(agentNodes.map((node) => node.id));
  const focus = options.focus && ids.has(options.focus) ? options.focus : undefined;
  const ancestors = (node: ExecutionNode) => {
    const chain: string[] = []; let parent = node.parentId;
    while (parent && ids.has(parent) && !chain.includes(parent) && parent !== node.id) {
      chain.push(parent); parent = model.byId.get(parent)?.parentId;
    }
    return chain;
  };
  const needle = options.search?.trim().toLocaleLowerCase();
  let nodes = agentNodes.filter((node) => {
    const parents = ancestors(node);
    return (!focus || node.id === focus || parents.includes(focus)) &&
      !parents.some((parent) => options.collapsed.has(parent) && parent !== focus);
  });
  if (needle || options.errorsOnly) {
    const matched = nodes.filter((node) => (!needle || `${node.title} ${node.id} ${node.kind}`.toLocaleLowerCase().includes(needle)) &&
      (!options.errorsOnly || ["failed", "error", "cancelled", "aborted", "unknown", "delivery_unknown", "incomplete"].includes(
        stepState(node.item.step, options.terminal, options.hasMore).status)));
    const keep = new Set(matched.flatMap((node) => [node.id, ...ancestors(node)]));
    nodes = nodes.filter((node) => keep.has(node.id));
  }
  const visible = new Set(nodes.map((node) => node.id));
  return { nodes, relations: model.relations.filter((edge) => visible.has(edge.source) && visible.has(edge.target)),
    hidden: ids.size - visible.size };
}
