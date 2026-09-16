import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Empty, Space, Spin, Tag } from "@arco-design/web-react";
import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow,
  type Edge, type Node, type NodeProps, type ReactFlowInstance, type Viewport } from "@xyflow/react";
import ELK from "elkjs/lib/elk-api";
import { visibleGraph, PROCESS_LABELS, type ExecutionModel, type ExecutionNode, type ExecutionRelation } from "./executionModel";
import { duration, stepDuration, stepState } from "./workbenchModel";
import { readSessionValue, saveSessionValue } from "./taskWorkspaceState";
import { executionLayoutGraph } from "./executionLayout";
import "@xyflow/react/dist/style.css";

type NodeData = { node: ExecutionNode; terminal: boolean; hasMore: boolean; highlighted: boolean;
  children: number; collapsed: boolean; onToggle: (id: string) => void; onFocus: (id: string) => void; onSelect: (id: string) => void };
type FlowNode = Node<NodeData, "execution">;
const ExecutionGraphNode = memo(function ExecutionGraphNode({ data, selected }: NodeProps<FlowNode>) {
  const state = stepState(data.node.item.step, data.terminal, data.hasMore);
  const usage = data.node.item.step.event.data?.usage as { total_tokens?: number } | undefined;
  return <div className={`execution-graph-node${selected ? " is-selected" : ""}${data.highlighted ? " is-neighbor" : ""}`}
    data-status={state.status} tabIndex={0} role="button" aria-label={`查看 ${data.node.title}`} aria-pressed={selected}
    onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); data.onSelect(data.node.id); } }}>
    <Handle type="target" position={Position.Left} />
    <div className="execution-graph-node-top"><span>{PROCESS_LABELS[data.node.kind] ?? "步骤"}</span><Tag size="small" color={state.color}>{state.label}</Tag></div>
    <strong title={data.node.title}>{data.node.title}</strong>
    <div className="execution-graph-node-meta"><span>{duration(stepDuration(data.node.item.step, data.terminal))}</span>
      {usage?.total_tokens != null && <span>{usage.total_tokens.toLocaleString()} Token</span>}</div>
    {!!data.children && <div className="execution-graph-node-actions"><button className="nodrag" onClick={(event) => {
      event.stopPropagation(); data.onToggle(data.node.id);
    }}>{data.collapsed ? "展开" : "收起"} {data.children} 个子调用</button>
      {["subagent", "workflow"].includes(data.node.kind) && <button className="nodrag" onClick={(event) => {
        event.stopPropagation(); data.onFocus(data.node.id);
      }}>聚焦</button>}</div>}
    <Handle type="source" position={Position.Right} />
  </div>;
});
const nodeTypes = { execution: ExecutionGraphNode };
const colors = { parent: "#86909c", call: "#165dff", input: "#008578" };
interface SavedGraph { collapsed: string[]; focus: string; viewport?: Viewport }
function restore(key: string): SavedGraph {
  const raw = readSessionValue(key) as Partial<SavedGraph> | null;
  const viewport = raw?.viewport;
  return { collapsed: Array.isArray(raw?.collapsed) ? raw.collapsed.filter((id): id is string => typeof id === "string") : [],
    focus: typeof raw?.focus === "string" ? raw.focus : "",
    viewport: viewport && [viewport.x, viewport.y, viewport.zoom].every(Number.isFinite) && viewport.zoom > 0 ? viewport : undefined };
}

export default function AgentExecutionGraph({ model, scope, selected, edgeId, terminal, hasMore, query, errorsOnly, storageKey, onSelect, onEdge }: {
  model: ExecutionModel; selected: string; edgeId: string; terminal: boolean; hasMore: boolean; query: string; errorsOnly: boolean;
  scope: ReadonlySet<string>; storageKey: string; onSelect: (id: string) => void; onEdge: (edge: ExecutionRelation) => void;
}) {
  const key = storageKey + ":graph";
  const [saved, setSaved] = useState(() => restore(key));
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [locate, setLocate] = useState(false);
  const api = useRef<ReactFlowInstance<FlowNode> | null>(null);
  const fitted = useRef(!!saved.viewport);
  const collapsed = useMemo(() => new Set(saved.collapsed), [saved.collapsed]);
  const graph = useMemo(() => visibleGraph(model, { collapsed, focus: saved.focus, search: query, errorsOnly, terminal, hasMore, scope }),
    [model, collapsed, saved.focus, query, errorsOnly, terminal, hasMore, scope]);
  // Only identity/topology affects layout. Streaming text and token updates never restart the worker.
  const topology = JSON.stringify({ nodes: graph.nodes.map((node) => node.id),
    edges: graph.relations.map((edge) => [edge.id, edge.source, edge.target]) });
  useEffect(() => saveSessionValue(key, saved), [key, saved]);
  useEffect(() => {
    const structure = JSON.parse(topology) as { nodes: string[]; edges: string[][] };
    if (!structure.nodes.length) { setBusy(false); return; }
    let cancelled = false;
    const elk = new ELK({ workerFactory: () => new Worker(new URL("elkjs/lib/elk-worker.min.js", import.meta.url)) });
    setBusy(true); setError("");
    void elk.layout(executionLayoutGraph(structure.nodes, structure.edges)).then((result) => {
      if (cancelled) return;
      setPositions(Object.fromEntries((result.children ?? []).map((node) => [node.id, { x: node.x ?? 0, y: node.y ?? 0 }])));
      setBusy(false);
    }).catch((reason) => { if (!cancelled) { setBusy(false); setError(`图布局失败：${String(reason)}。可切换执行列表继续查看。`); } });
    return () => { cancelled = true; elk.terminateWorker(); };
  }, [topology, revision]);
  useEffect(() => {
    if (busy || !api.current || !Object.keys(positions).length) return;
    const frame = requestAnimationFrame(() => {
      if (locate && positions[selected]) {
        const position = positions[selected];
        void api.current?.setCenter(position.x + 125, position.y + 65, { zoom: 0.9, duration: 200 });
        setLocate(false);
      } else if (!fitted.current) { fitted.current = true; void api.current?.fitView({ padding: 0.15, minZoom: 0.6, maxZoom: 1 }); }
    });
    return () => cancelAnimationFrame(frame);
  }, [positions, busy, locate, selected]);
  const toggle = (id: string) => setSaved((value) => ({ ...value,
    collapsed: value.collapsed.includes(id) ? value.collapsed.filter((item) => item !== id) : [...value.collapsed, id] }));
  const focus = (id: string) => { setSaved((value) => ({ ...value, focus: id })); fitted.current = false; onSelect(id); };
  const scopedRelations = model.relations.filter((edge) => scope.has(edge.source) && scope.has(edge.target));
  const neighbors = new Set(scopedRelations.filter((edge) => edge.source === selected || edge.target === selected).flatMap((edge) => [edge.source, edge.target]));
  const children = new Map<string, number>();
  for (const node of model.nodes) if (scope.has(node.id) && node.parentId && scope.has(node.parentId)) {
    children.set(node.parentId, (children.get(node.parentId) ?? 0) + 1);
  }
  const nodes: FlowNode[] = graph.nodes.map((node, index) => ({ id: node.id, type: "execution", selected: node.id === selected,
    position: positions[node.id] ?? { x: index % 4 * 300, y: Math.floor(index / 4) * 160 },
    data: { node, terminal, hasMore, highlighted: neighbors.has(node.id), children: children.get(node.id) ?? 0,
      collapsed: collapsed.has(node.id), onToggle: toggle, onFocus: focus, onSelect },
  }));
  const edges: Edge[] = graph.relations.map((relation) => ({ id: relation.id, source: relation.source, target: relation.target,
    type: "smoothstep", label: relation.label, selected: relation.id === edgeId,
    style: { stroke: colors[relation.kind], strokeWidth: relation.id === edgeId || relation.source === selected || relation.target === selected ? 2.5 : 1.2,
      strokeDasharray: relation.kind === "parent" ? "5 4" : undefined },
    markerEnd: { type: MarkerType.ArrowClosed, color: colors[relation.kind] },
    labelStyle: { fontSize: 11 }, ariaLabel: `${relation.label}：${model.byId.get(relation.source)?.title} → ${model.byId.get(relation.target)?.title}`,
  }));
  return <div className="execution-graph-shell"><div className="execution-graph-tools"><Space wrap size="mini">
    <Button size="mini" onClick={() => void api.current?.fitView({ padding: 0.15, maxZoom: 1 })}>适应画布</Button>
    <Button size="mini" disabled={!selected} onClick={() => { setSaved((value) => ({ ...value, focus: "", collapsed: [] })); setLocate(true); }}>定位所选</Button>
    <Button size="mini" onClick={() => setRevision((value) => value + 1)}>重新排列</Button>
    {saved.focus && <Button size="mini" onClick={() => { setSaved((value) => ({ ...value, focus: "" })); fitted.current = false; }}>返回完整 Agent 图</Button>}
  </Space><span className="obs-muted">{graph.nodes.length} 个节点{graph.hidden > 0 ? ` · ${graph.hidden} 个已折叠或筛选` : ""}{hasMore ? " · 记录未加载完整" : ""}</span></div>
    {error && <Alert type="error" content={error} />}
    {saved.focus && !scope.has(saved.focus) && <Alert type="info" content="聚焦的调用不在当前 Agent 阶段，可返回完整图。" />}
    <div className="execution-graph" aria-label="Agent 执行有向图" aria-busy={busy}>
      {busy && <div className="execution-graph-loading"><Spin size={16} /> 正在排列节点</div>}
      {nodes.length ? <ReactFlow<FlowNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        onInit={(instance) => { api.current = instance; }} defaultViewport={saved.viewport} minZoom={0.05} maxZoom={1.8}
        nodesDraggable={false} nodesConnectable={false} nodesFocusable={false} edgesReconnectable={false} deleteKeyCode={null}
        onlyRenderVisibleElements panOnScroll={false} zoomOnScroll={false} zoomOnPinch
        onMoveEnd={(_, viewport) => setSaved((value) => ({ ...value, viewport }))}
        onNodeClick={(_, node) => onSelect(node.id)} onEdgeClick={(_, edge) => {
          const relation = graph.relations.find((item) => item.id === edge.id); if (relation) onEdge(relation);
        }}><Background gap={24} /><Controls showInteractive={false} /><MiniMap pannable zoomable /></ReactFlow> :
        <Empty description={hasMore ? "尚未取得匹配的 Agent 调用" : "没有匹配的 Agent 调用"} />}
    </div>
    <div className="execution-graph-legend"><span>┄ 父子归属</span><span>→ 触发调用</span><span>→ 进入上下文</span><span>点击连线查看关系依据</span></div>
  </div>;
}
