import { Component, lazy, Suspense, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { Alert, Button, Checkbox, Empty, Input, Radio, Space, Spin, Tag } from "@arco-design/web-react";
import { buildExecutionModel, layerSummaries, PROCESS_LABELS, type ExecutionNode, type ExecutionRelation } from "./executionModel";
import { RUNTIME_LAYERS, dateTime, duration, stepDuration, stepState, type FlowItem, type RuntimeLayer } from "./workbenchModel";
import { DetailScope, Disclosure, ObservationPayload } from "./ObservationContent";
import StructuredData from "./StructuredData";
import { readSessionValue, saveSessionValue } from "./taskWorkspaceState";
import "../../styles/execution-workspace.css";

const AgentExecutionGraph = lazy(() => import("./AgentExecutionGraph"));
class GraphBoundary extends Component<{ children: ReactNode; onTree: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <Alert type="error" content="图组件加载失败，可刷新页面或继续查看调用树。"
      action={<Button size="mini" onClick={this.props.onTree}>查看调用树</Button>} /> : this.props.children;
  }
}
type View = "stages" | "graph" | "tree";
interface Selection { view: View; selected: string; pinned: string; layer: RuntimeLayer | "" }
function restore(key: string): Selection {
  const value = readSessionValue(key) as Partial<Selection> | null;
  return { view: value && ["stages", "graph", "tree"].includes(value.view ?? "") ? value.view! : "stages",
    selected: typeof value?.selected === "string" ? value.selected : "", pinned: typeof value?.pinned === "string" ? value.pinned : "",
    layer: value?.layer && value.layer in RUNTIME_LAYERS ? value.layer : "" };
}
function NodeRow({ node, selected, terminal, hasMore, tree, onSelect }: {
  node: ExecutionNode; selected: boolean; terminal: boolean; hasMore: boolean; tree?: boolean; onSelect: () => void;
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
      {node.item.step.agentName && <span>{node.item.step.agentName}</span>}
      <time>{dateTime(node.item.step.start?.created_at ?? node.item.step.event.created_at)}</time>
      <span>{duration(stepDuration(node.item.step, terminal))}</span>
      {usage?.total_tokens != null && <span>{usage.total_tokens.toLocaleString()} Token</span>}</span>
  </button>;
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
  const [expandedCanvas, setExpandedCanvas] = useState(false);
  const [limit, setLimit] = useState(() => {
    const saved = readSessionValue(storageKey + ":list-limit");
    return typeof saved === "number" && Number.isSafeInteger(saved) && saved >= 150 ? saved : 150;
  });
  const model = useMemo(() => buildExecutionModel(flow), [flow]);
  const summary = layerSummaries(model, terminal, hasMore);
  useEffect(() => saveSessionValue(storageKey, selection), [storageKey, selection]);
  useEffect(() => saveSessionValue(storageKey + ":list-limit", limit), [storageKey, limit]);
  useEffect(() => {
    if (!selection.selected && model.nodes.length) {
      const initial = model.nodes.find((node) => stepState(node.item.step, terminal, hasMore).status === "failed") ??
        model.nodes.find((node) => stepState(node.item.step, terminal, hasMore).status === "running") ?? model.nodes[0];
      setSelection((value) => ({ ...value, selected: value.selected || initial.id }));
    }
  }, [selection.selected, model, terminal, hasMore]);
  const selectNode = (id: string) => { setSelection((value) => ({ ...value, selected: id })); setEdgeId(""); };
  const selected = model.byId.get(selection.selected);
  const pinned = model.byId.get(selection.pinned);
  const edge = model.relations.find((item) => item.id === edgeId);
  const scope = { instanceId, runId, expired, active };
  const matches = (node: ExecutionNode) => (!selection.layer || node.item.layer === selection.layer) &&
    (!query.trim() || `${node.title} ${node.id} ${node.kind}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) &&
    (!errorsOnly || ["failed", "error", "unknown", "delivery_unknown", "incomplete", "cancelled", "aborted"].includes(stepState(node.item.step, terminal, hasMore).status));
  const visibleNodes = model.nodes.filter(matches);
  const groups = model.stages.map((group) => ({ ...group, nodes: group.nodes.filter(matches) }))
    .filter((group) => group.nodes.length || group.stage && matches(group.stage));
  const related = selected ? model.relations.filter((relation) => relation.source === selected.id || relation.target === selected.id) : [];
  const selectEdge = (relation: ExecutionRelation) => { setEdgeId(relation.id); setSelection((value) => ({ ...value, selected: relation.target })); };
  const jumpToInspector = () => document.getElementById("execution-inspector")?.scrollIntoView({ block: "start", behavior: "smooth" });
  return <section className={"execution-workspace" + (expandedCanvas ? " is-expanded" : "")} aria-label="四层任务工作台">
    <div className="execution-layer-overview" aria-label="四层总览">{summary.map((item, index) =>
      <button key={item.layer} type="button" data-runtime-layer={item.layer} aria-pressed={selection.layer === item.layer}
        onClick={() => { setSelection((value) => ({ ...value, layer: value.layer === item.layer ? "" : item.layer,
          view: item.layer === "agent" ? "graph" : "stages" })); setLimit(150); }}>
        <span className="execution-layer-name"><small>0{index + 1}</small><strong>{({ channel: "Channel", gateway: "Gateway", application: "Application", agent: "Agent" })[item.layer]}</strong></span>
        <span>{RUNTIME_LAYERS[item.layer]} · {item.stages} 段 / {item.nodes - item.stages} 步</span>
        <span className={item.failed ? "is-error" : ""}>{item.label}</span>
      </button>)}</div>
    <div className="execution-toolbar"><Radio.Group type="button" size="small" value={selection.view}
      onChange={(view: View) => { setSelection((value) => ({ ...value, view, layer: view === "graph" ? "" : value.layer })); setLimit(150); }}
      options={[{ label: "四层过程", value: "stages" }, { label: "Agent 图", value: "graph" }, { label: "调用树", value: "tree" }]} />
      <Space wrap size="mini"><Button size="mini" onClick={() => {
        const current = model.nodes.find((node) => stepState(node.item.step, terminal, hasMore).status === "running") ??
          model.nodes.find((node) => stepState(node.item.step, terminal, hasMore).status === "failed");
        if (current) { selectNode(current.id); setSelection((value) => ({ ...value, layer: "" })); setQuery(""); setErrorsOnly(false); setLimit(model.nodes.length); }
      }}>定位当前 / 异常</Button><Button size="mini" onClick={() => setExpandedCanvas(!expandedCanvas)}>{expandedCanvas ? "退出放大" : "放大工作台"}</Button></Space>
    </div>
    <div className="execution-filters"><Input.Search size="small" allowClear aria-label="搜索运行步骤" placeholder="搜索步骤、模型或调用 ID" value={query}
      onChange={(value) => { setQuery(value); setLimit(150); }} /><Checkbox checked={errorsOnly} onChange={setErrorsOnly}>只看异常</Checkbox>
      {selection.layer && <Button type="text" size="mini" onClick={() => setSelection((value) => ({ ...value, layer: "" }))}>清除 {RUNTIME_LAYERS[selection.layer]} 筛选</Button>}
      <span className="obs-muted">已加载 {model.nodes.length} 个阶段 / 步骤{hasMore ? " · 还有后续记录" : ""}</span>
    </div>
    <div className="execution-columns">
      <div className="execution-navigation">
        {selection.view === "graph" ? <GraphBoundary onTree={() => setSelection((value) => ({ ...value, view: "tree" }))}><Suspense fallback={<Spin tip="正在加载 Agent 图…" />}>
          <AgentExecutionGraph model={model} storageKey={storageKey} selected={selection.selected} edgeId={edgeId}
            query={query} errorsOnly={errorsOnly} terminal={terminal} hasMore={hasMore} onSelect={selectNode} onEdge={selectEdge} />
        </Suspense></GraphBoundary> : selection.view === "tree" ? <div className="execution-tree" aria-label="调用树">
          {visibleNodes.slice(0, limit).map((node) => <NodeRow key={node.id} node={node} selected={node.id === selection.selected}
            terminal={terminal} hasMore={hasMore} onSelect={() => selectNode(node.id)} tree />)}
          {visibleNodes.length > limit && <Button onClick={() => setLimit(limit + 150)}>显示后续步骤（剩余 {visibleNodes.length - limit}）</Button>}
          {!visibleNodes.length && <Empty description="没有匹配的调用" />}
        </div> : <div className="execution-stages" aria-label="四层执行过程">
          {groups.slice(0, limit).map((group, index) => <section className="execution-stage-group" key={group.stage?.id ?? group.nodes[0]?.id ?? index}
            data-runtime-layer={group.stage?.item.layer ?? group.nodes[0]?.item.layer}>
            {group.stage ? <NodeRow node={group.stage} selected={group.stage.id === selection.selected}
              terminal={terminal} hasMore={hasMore} onSelect={() => selectNode(group.stage!.id)} /> :
              <div className="execution-unbound">{hasMore ? "所属阶段尚未取得" : "独立记录 / 所属阶段未记录"}</div>}
            {group.nodes.slice(0, limit).map((node) => <NodeRow key={node.id} node={node} selected={node.id === selection.selected}
              terminal={terminal} hasMore={hasMore} onSelect={() => selectNode(node.id)} tree />)}
            {group.nodes.length > limit && <Button size="mini" onClick={() => setLimit(limit + 150)}>显示段内后续步骤（剩余 {group.nodes.length - limit}）</Button>}
          </section>)}
          {groups.length > limit && <Button onClick={() => setLimit(limit + 150)}>显示后续阶段</Button>}
          {!groups.length && <Empty description={hasMore ? "尚未取得匹配过程，可加载后续记录" : "没有匹配的四层过程"} />}
        </div>}
        {selected && <Button className="execution-inspector-jump" size="small" onClick={jumpToInspector}>查看所选步骤详情 ↓</Button>}
      </div>
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
          {edge.kind === "input" && <><ObservationPayload {...scope} showRaw={false} reference={model.byId.get(edge.source)?.item.step.finish?.body_ref}
            title="来源：交给模型的工具结果" select={(value) => (value as Record<string, unknown>)?.model_result} />
            <ObservationPayload {...scope} showRaw={false} reference={edge.event.body_ref} captureState={edge.event.body_state} title={`去向：输入消息 ${edge.messageIndex! + 1}`}
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
