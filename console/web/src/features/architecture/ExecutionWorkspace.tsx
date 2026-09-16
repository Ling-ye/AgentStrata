import { Component, lazy, Suspense, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { Alert, Button, Checkbox, Empty, Input, Radio, Space, Spin, Tag } from "@arco-design/web-react";
import {
  agentExecutionScope,
  buildExecutionModel,
  executionStageProjection,
  layerSummaries,
  PROCESS_LABELS,
  visibleGraph,
  type AgentExecutionScope,
  type ExecutionModel,
  type ExecutionNode,
  type ExecutionRelation,
  type RuntimeStageProjection,
} from "./executionModel";
import { RUNTIME_LAYERS, dateTime, duration, stepDuration, stepState, type FlowItem, type RuntimeLayer } from "./workbenchModel";
import { DetailScope, Disclosure, ObservationPayload } from "./ObservationContent";
import StructuredData from "./StructuredData";
import { readSessionValue, saveSessionValue } from "./taskWorkspaceState";
import "../../styles/execution-workspace.css";

const AgentExecutionGraph = lazy(() => import("./AgentExecutionGraph"));
const LAYERS = ["channel", "gateway", "application", "agent"] as const;
const LAYER_TITLES: Record<RuntimeLayer, string> = {
  channel: "Channel", gateway: "Gateway", application: "Application", agent: "Agent",
};
const FAILED_STATES = new Set(["failed", "error", "unknown", "delivery_unknown", "incomplete", "cancelled", "aborted"]);
const RUNNING_STATES = new Set(["running", "pending", "submitting"]);

class GraphBoundary extends Component<{ children: ReactNode; onList: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <Alert type="error" content="图组件加载失败，可刷新页面或切换执行列表。"
      action={<Button size="mini" onClick={this.props.onList}>查看执行列表</Button>} /> : this.props.children;
  }
}

type AgentView = "list" | "graph";
interface Selection { selected: string; pinned: string; agentView: AgentView }
function restore(key: string): Selection {
  const value = readSessionValue(key) as Partial<Selection> | null;
  return { selected: typeof value?.selected === "string" ? value.selected : "",
    pinned: typeof value?.pinned === "string" ? value.pinned : "",
    agentView: value?.agentView === "graph" ? "graph" : "list" };
}

function NodeRow({ node, selected, terminal, hasMore, tree, childCount, onSelect }: {
  node: ExecutionNode; selected: boolean; terminal: boolean; hasMore: boolean; tree?: boolean; childCount?: number; onSelect: () => void;
}) {
  const state = stepState(node.item.step, terminal, hasMore);
  const usage = node.item.step.event.data?.usage as { total_tokens?: number } | undefined;
  return <button type="button" className={`execution-row${selected ? " is-selected" : ""}`}
    style={tree ? { "--tree-depth": Math.min(node.item.step.depth, 8) } as CSSProperties : undefined}
    data-step-key={node.id} data-runtime-layer={node.item.layer} data-status={state.status}
    aria-pressed={selected} onClick={onSelect}>
    <span className="execution-row-title"><span className="execution-node-kind">{PROCESS_LABELS[node.kind] ?? "步骤"}</span>
      <strong>{node.title}</strong><Tag size="small" color={state.color}>{state.label}</Tag></span>
    <span className="execution-row-meta"><span>{node.item.layer ? RUNTIME_LAYERS[node.item.layer] : "职责未记录"}</span>
      {childCount != null && <span>{childCount} 个内部调用</span>}
      {node.item.step.agentName && <span>{node.item.step.agentName}</span>}
      <time>{dateTime(node.item.step.start?.created_at ?? node.item.step.event.created_at)}</time>
      <span>{duration(stepDuration(node.item.step, terminal))}</span>
      {usage?.total_tokens != null && <span>{usage.total_tokens.toLocaleString()} Token</span>}</span>
  </button>;
}

function RuntimeStageGrid({ projection, summary, activeStageId, selectedId, terminal, hasMore, limit, onSelect, onMore, onLocate }: {
  projection: RuntimeStageProjection;
  summary: ReturnType<typeof layerSummaries>;
  activeStageId: string;
  selectedId: string;
  terminal: boolean;
  hasMore: boolean;
  limit: number;
  onSelect: (id: string) => void;
  onMore: () => void;
  onLocate: () => void;
}) {
  return <section className="execution-runtime" aria-label="四层运行轨迹">
    <header className="execution-runtime-heading"><div><strong>四层运行轨迹</strong>
      <span className="obs-muted">按真实交接顺序从上向下排列</span></div>
      <Button size="mini" disabled={!projection.stages.length} onClick={onLocate}>定位当前 / 异常</Button></header>
    <div className="execution-runtime-grid">
      <div className="execution-runtime-grid-header"><span>顺序</span>{summary.map((item) =>
        <span key={item.layer} className="execution-runtime-layer" data-runtime-layer={item.layer}>
          <strong>{LAYER_TITLES[item.layer]}</strong><small>{RUNTIME_LAYERS[item.layer]} · {item.stages} 段</small>
          <small className={item.failed ? "is-error" : ""}>{item.label}</small>
        </span>)}</div>
      {projection.stages.map((group, index) => {
        const layer = group.stage.item.layer;
        const column = layer ? LAYERS.indexOf(layer) + 2 : 2;
        return <div className="execution-runtime-row" data-runtime-layer={layer} key={group.stage.id}>
          <span className="execution-runtime-sequence">{String(index + 1).padStart(2, "0")}</span>
          <div className="execution-runtime-stage" style={{ gridColumn: column }}>
            <NodeRow node={group.stage} selected={group.stage.id === activeStageId} terminal={terminal} hasMore={hasMore}
              childCount={group.nodes.length} onSelect={() => onSelect(group.stage.id)} />
          </div>
        </div>;
      })}
      {!projection.stages.length && <Empty description={hasMore ? "四层阶段尚未取得" : "没有已记录的四层阶段"} />}
    </div>
    {!!projection.unbound.length && <section className="execution-unbound-section" aria-label="未归属运行记录">
      <header><strong>观测缺口</strong><span className="obs-muted">保留真实记录，不推测所属阶段</span></header>
      {projection.unbound.map((group) => <section className="execution-unbound-group" key={group.key} data-runtime-layer={group.layer}>
        <div className="execution-unbound-heading"><strong>{group.missingStage ? hasMore ? "所属阶段尚未取得" : "所属阶段未记录" : "未归属记录"}</strong>
          <span>{group.layer ? RUNTIME_LAYERS[group.layer] : "职责未记录"} · {group.nodes.length} 条</span></div>
        <div className="execution-agent-list">{group.nodes.slice(0, limit).map((node) =>
          <NodeRow key={node.id} node={node} selected={node.id === selectedId} terminal={terminal} hasMore={hasMore}
            tree onSelect={() => onSelect(node.id)} />)}</div>
        {group.nodes.length > limit && <Button size="mini" onClick={onMore}>显示后续记录（剩余 {group.nodes.length - limit}）</Button>}
      </section>)}
    </section>}
  </section>;
}

function AgentExecutionPanel({ model, scope, selected, edgeId, view, query, errorsOnly, terminal, hasMore, storageKey,
  limit, expanded, onSelect, onEdge, onView, onQuery, onErrorsOnly, onMore, onExpanded }: {
  model: ExecutionModel;
  scope: AgentExecutionScope;
  selected: string;
  edgeId: string;
  view: AgentView;
  query: string;
  errorsOnly: boolean;
  terminal: boolean;
  hasMore: boolean;
  storageKey: string;
  limit: number;
  expanded: boolean;
  onSelect: (id: string) => void;
  onEdge: (edge: ExecutionRelation) => void;
  onView: (view: AgentView) => void;
  onQuery: (value: string) => void;
  onErrorsOnly: (value: boolean) => void;
  onMore: () => void;
  onExpanded: () => void;
}) {
  const effectiveView: AgentView = scope.root ? view : "list";
  const list = visibleGraph(model, { collapsed: new Set(), search: query, errorsOnly, terminal, hasMore, scope: scope.ids });
  const nodes = list.nodes.filter((node) => node.id !== scope.root?.id);
  const locate = () => {
    const current = scope.nodes.find((node) => FAILED_STATES.has(stepState(node.item.step, terminal, hasMore).status)) ??
      scope.nodes.find((node) => RUNNING_STATES.has(stepState(node.item.step, terminal, hasMore).status));
    if (current) { onQuery(""); onErrorsOnly(false); onSelect(current.id); }
  };
  return <section className="execution-agent-panel" aria-label="Agent 执行过程">
    <header className="execution-agent-heading"><div><strong>{scope.root ? "Agent 执行" : "未归属的 Agent 记录"}</strong>
      <span className="obs-muted">{Math.max(0, scope.nodes.length - (scope.root ? 1 : 0))} 个内部调用</span></div>
      {scope.root && <Radio.Group type="button" size="small" value={effectiveView} onChange={onView}
        options={[{ label: "执行列表", value: "list" }, { label: "调用关系图", value: "graph" }]} />}</header>
    <div className="execution-agent-toolbar"><Input.Search size="small" allowClear aria-label="搜索 Agent 调用"
      placeholder="搜索模型、工具或调用 ID" value={query} onChange={onQuery} />
      <Checkbox checked={errorsOnly} onChange={onErrorsOnly}>只看异常</Checkbox>
      <Space wrap size="mini"><Button size="mini" disabled={!scope.nodes.length} onClick={locate}>定位当前 / 异常</Button>
        {effectiveView === "graph" && <Button size="mini" onClick={onExpanded}>{expanded ? "退出放大" : "放大工作台"}</Button>}</Space>
      <span className="obs-muted">已加载 {scope.nodes.length} 个 Agent 节点{hasMore ? " · 还有后续记录" : ""}</span></div>
    {effectiveView === "graph" ? <GraphBoundary key={scope.key} onList={() => onView("list")}>
      <Suspense fallback={<Spin tip="正在加载 Agent 图…" />}><AgentExecutionGraph model={model} scope={scope.ids}
        storageKey={`${storageKey}:agent:${scope.key}`} selected={selected} edgeId={edgeId} query={query} errorsOnly={errorsOnly}
        terminal={terminal} hasMore={hasMore} onSelect={onSelect} onEdge={onEdge} /></Suspense>
    </GraphBoundary> : <div className="execution-agent-list" aria-label="Agent 执行列表">
      {nodes.slice(0, limit).map((node) => <NodeRow key={node.id} node={node} selected={node.id === selected}
        terminal={terminal} hasMore={hasMore} tree onSelect={() => onSelect(node.id)} />)}
      {nodes.length > limit && <Button onClick={onMore}>显示后续调用（剩余 {nodes.length - limit}）</Button>}
      {!nodes.length && <Empty description={hasMore ? "尚未取得匹配的 Agent 调用" : "没有匹配的 Agent 调用"} />}
    </div>}
  </section>;
}

export default function ExecutionWorkspace({ flow, instanceId, runId, terminal, hasMore, expired, active, renderDetail }: {
  flow: FlowItem[]; instanceId: string; runId: string; terminal: boolean; hasMore: boolean; expired: boolean; active: boolean;
  renderDetail: (item: FlowItem) => ReactNode;
}) {
  const storageKey = `obs:execution:${instanceId}:${runId}`;
  const [selection, setSelection] = useState(() => restore(storageKey));
  const [query, setQuery] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [edgeId, setEdgeId] = useState("");
  const [expandedAgent, setExpandedAgent] = useState(false);
  const [limit, setLimit] = useState(() => {
    const saved = readSessionValue(storageKey + ":list-limit");
    return typeof saved === "number" && Number.isSafeInteger(saved) && saved >= 150 ? saved : 150;
  });
  const model = useMemo(() => buildExecutionModel(flow), [flow]);
  const projection = useMemo(() => executionStageProjection(model), [model]);
  const summary = layerSummaries(model, terminal, hasMore);
  const agentScope = useMemo(() => agentExecutionScope(model, selection.selected), [model, selection.selected]);
  useEffect(() => saveSessionValue(storageKey, selection), [storageKey, selection]);
  useEffect(() => saveSessionValue(storageKey + ":list-limit", limit), [storageKey, limit]);
  useEffect(() => {
    if (selection.selected && (model.byId.has(selection.selected) || hasMore)) return;
    const failed = projection.stages.find((group) => [group.stage, ...group.nodes].some((node) =>
      FAILED_STATES.has(stepState(node.item.step, terminal, hasMore).status)));
    const running = projection.stages.find((group) => [group.stage, ...group.nodes].some((node) =>
      RUNNING_STATES.has(stepState(node.item.step, terminal, hasMore).status)));
    const initial = failed?.stage ?? running?.stage ?? projection.stages[0]?.stage ?? projection.unbound[0]?.nodes[0] ?? model.nodes[0];
    if (initial) setSelection((value) => value.selected === initial.id ? value : { ...value, selected: initial.id });
  }, [selection.selected, model, projection, terminal, hasMore]);
  const selectNode = (id: string) => { setSelection((value) => ({ ...value, selected: id })); setEdgeId(""); };
  const selectStage = (id: string) => {
    selectNode(id); setQuery(""); setErrorsOnly(false); setLimit(150); setExpandedAgent(false);
  };
  const selected = model.byId.get(selection.selected);
  const pinned = model.byId.get(selection.pinned);
  const selectedStage = model.stages.find((group) => group.stage?.id === selection.selected || group.nodes.some((node) => node.id === selection.selected))?.stage;
  const edge = model.relations.find((item) => item.id === edgeId);
  const bodyScope = { instanceId, runId, expired, active };
  const related = selected ? model.relations.filter((relation) => relation.source === selected.id || relation.target === selected.id) : [];
  const selectEdge = (relation: ExecutionRelation) => { setEdgeId(relation.id); setSelection((value) => ({ ...value, selected: relation.target })); };
  const locateStage = () => {
    const failed = projection.stages.find((group) => [group.stage, ...group.nodes].some((node) =>
      FAILED_STATES.has(stepState(node.item.step, terminal, hasMore).status)));
    const running = projection.stages.find((group) => [group.stage, ...group.nodes].some((node) =>
      RUNNING_STATES.has(stepState(node.item.step, terminal, hasMore).status)));
    const target = failed?.stage ?? running?.stage ?? projection.stages[0]?.stage;
    if (target) selectStage(target.id);
  };
  const jumpToInspector = () => document.getElementById("execution-inspector")?.scrollIntoView({ block: "start", behavior: "smooth" });
  return <section className="execution-workspace" aria-label="四层任务工作台">
    <RuntimeStageGrid projection={projection} summary={summary} activeStageId={selectedStage?.id ?? ""}
      selectedId={selection.selected} terminal={terminal} hasMore={hasMore} limit={limit}
      onSelect={selectStage} onMore={() => setLimit(limit + 150)} onLocate={locateStage} />
    {selected && <Button className="execution-inspector-jump" size="small" onClick={jumpToInspector}>查看所选步骤详情 ↓</Button>}
    <div className={`execution-detail-layout${agentScope ? " has-agent" : ""}${expandedAgent && agentScope ? " is-expanded" : ""}`}>
      {agentScope && <AgentExecutionPanel model={model} scope={agentScope} selected={selection.selected} edgeId={edgeId}
        view={selection.agentView} query={query} errorsOnly={errorsOnly} terminal={terminal} hasMore={hasMore}
        storageKey={storageKey} limit={limit} expanded={expandedAgent} onSelect={selectNode} onEdge={selectEdge}
        onView={(agentView) => { setSelection((value) => ({ ...value, agentView })); if (agentView === "list") setExpandedAgent(false); }}
        onQuery={(value) => { setQuery(value); setLimit(150); }} onErrorsOnly={(value) => { setErrorsOnly(value); setLimit(150); }}
        onMore={() => setLimit(limit + 150)} onExpanded={() => setExpandedAgent((value) => !value)} />}
      <aside className="execution-inspector" id="execution-inspector" aria-label="结构化详情检查器">
        <header className="obs-pane-heading"><strong>结构化详情</strong>{selected && <Button size="mini"
          onClick={() => setSelection((value) => ({ ...value, pinned: value.pinned === value.selected ? "" : value.selected }))}>
          {selection.pinned === selection.selected ? "取消固定" : "固定此步骤对照"}</Button>}</header>
        {pinned && pinned.id !== selected?.id && <div className="execution-pinned-label">已固定：{pinned.title}<Button size="mini" type="text"
          onClick={() => setSelection((value) => ({ ...value, pinned: "" }))}>取消固定</Button></div>}
        {edge && <DetailScope id={`relation:${edge.id}`}><section className="execution-relation-detail" aria-label="关系依据">
          <strong>{edge.label}</strong><p><button onClick={() => selectNode(edge.source)}>{model.byId.get(edge.source)?.title}</button> → <button onClick={() => selectNode(edge.target)}>{model.byId.get(edge.target)?.title}</button></p>
          <StructuredData value={{ field: edge.field, value: edge.value, event_sequence: edge.event.seq,
            ...(edge.messageIndex == null ? {} : { message_pointer: `/effective_messages/${edge.messageIndex}` }) }} />
          {edge.kind === "input" && <><ObservationPayload {...bodyScope} showRaw={false} reference={model.byId.get(edge.source)?.item.step.finish?.body_ref}
            title="来源：交给模型的工具结果" select={(value) => (value as Record<string, unknown>)?.model_result} />
            <ObservationPayload {...bodyScope} showRaw={false} reference={edge.event.body_ref} captureState={edge.event.body_state} title={`去向：输入消息 ${edge.messageIndex! + 1}`}
              select={(value) => ((value as Record<string, unknown>)?.effective_messages as unknown[])?.[edge.messageIndex!]} messages /></>}
        </section></DetailScope>}
        {!selected && <Empty description={selection.selected && hasMore ? "所选步骤尚未加载，请加载后续记录" : "选择一个阶段或调用查看输入输出"} />}
        <div className="execution-comparison">{[selected, pinned && pinned.id !== selected?.id ? pinned : undefined].filter((node): node is ExecutionNode => !!node).map((node) =>
          <div key={node.id} className="execution-inspected-node"><DetailScope id={`inspect:${node.id}`}>{renderDetail(node.item)}</DetailScope></div>)}</div>
        {selected && <DetailScope id={`relations:${selected.id}`}><Disclosure title={`来源与去向（${related.length}）`}>
          {related.map((relation) => <button className="execution-relation-row" key={relation.id} onClick={() => selectEdge(relation)}>
            <span>{relation.label}</span><span>{model.byId.get(relation.source)?.title} → {model.byId.get(relation.target)?.title}</span>
          </button>)}
          {!related.length && <p className="obs-muted">没有已确认的关联</p>}
        </Disclosure></DetailScope>}
        {selected && model.gaps.filter((gap) => gap.nodeId === selected.id).map((gap, index) => <Alert key={index} type="warning" content={gap.message + (hasMore ? "，可继续加载记录。" : "，不会推断关联。")} />)}
      </aside>
    </div>
  </section>;
}
