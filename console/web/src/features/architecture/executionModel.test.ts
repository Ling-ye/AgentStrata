import { describe, expect, it } from "vitest";
import { agentExecutionScope, buildExecutionModel, executionStageProjection, layerSummaries, spanKey, visibleGraph } from "./executionModel";
import type { GatewayObservation } from "./model";
import { buildRunView } from "./workbenchModel";

const event = (seq: number, span: string, parent: string | undefined, kind: string, data = {}, extra: Partial<GatewayObservation> = {}): GatewayObservation => ({
  seq, kind, trace_id: "trace", span_id: span, parent_span_id: parent, phase: "finish", status: "succeeded", created_at: seq,
  ...extra, data: { flow_version: 1, runtime_layer: "agent", stage_span_id: "stage", process_kind: kind === "ToolFinished" ? "tool" : kind === "LlmCallFinished" ? "model_call" : "event", ...data },
});
const base = () => [
  event(1, "stage", undefined, "RuntimeStageFinished", { operation: "agent.execute" }),
  event(2, "agent", "stage", "SpanFinished", { process_kind: "agent" }),
  event(3, "model1", "agent", "LlmCallFinished", { iteration: 0 }),
  event(4, "tool", "agent", "ToolFinished", { tool_call_id: "call-a", model_span_id: "model1", name: "lookup" }),
  event(5, "model2", "agent", "ContextSnapshotPrepared", { snapshot_id: "context", input_tool_refs: [{ tool_call_id: "call-a", message_index: 3 }] }, { phase: "" }),
  event(6, "model2", "agent", "LlmCallFinished", { iteration: 1, context_snapshot_id: "context" }),
];
const project = (events: GatewayObservation[]) => buildExecutionModel(buildRunView(events).flow);

describe("shared execution projection", () => {
  it("uses explicit trigger and actual-input references without inventing sequential edges", () => {
    const model = project(base());
    expect(model.nodes).toHaveLength(5);
    expect(model.relations.filter((edge) => edge.kind === "call")).toMatchObject([{ source: spanKey("trace", "model1"), target: spanKey("trace", "tool") }]);
    expect(model.relations.filter((edge) => edge.kind === "input")).toMatchObject([{ source: spanKey("trace", "tool"), target: spanKey("trace", "model2"), messageIndex: 3, event: { seq: 5 } }]);
    expect(model.relations.some((edge) => edge.source === spanKey("trace", "model1") && edge.target === spanKey("trace", "model2"))).toBe(false);
    expect(model.stages[0].nodes).toHaveLength(4);
  });
  it("merges paged starts/finishes and duplicate event identities into one node", () => {
    const records = base();
    const model = project([event(0, "tool", "agent", "ToolStarted", { tool_call_id: "call-a" }, { phase: "start", status: "running" }), ...records, records[3]]);
    expect(model.nodes.filter((node) => node.item.step.event.span_id === "tool")).toHaveLength(1);
    expect(model.byId.get(spanKey("trace", "tool")!)?.item.step.start?.seq).toBe(0);
  });
  it("keeps same-name parallel calls separate and refuses ambiguous call IDs", () => {
    const model = project([...base(), event(7, "duplicate", "agent", "ToolFinished", { tool_call_id: "call-a", name: "lookup" })]);
    expect(model.nodes.filter((node) => node.kind === "tool")).toHaveLength(2);
    expect(model.relations.filter((edge) => edge.kind === "input")).toHaveLength(0);
    expect(model.gaps[0].message).toContain("歧义");
  });
  it("does not borrow tool calls from another trace or nested Agent scope", () => {
    const records = base().filter((row) => row.span_id !== "tool");
    records.push(event(7, "sub", "agent", "SpanFinished", { process_kind: "subagent", name: "subagent:research" }),
      event(8, "nested-tool", "sub", "ToolFinished", { tool_call_id: "call-a" }),
      event(9, "tool", "agent", "ToolFinished", { tool_call_id: "call-a" }, { trace_id: "other" }));
    const model = project(records);
    expect(model.relations.filter((edge) => edge.kind === "input")).toHaveLength(0);
    expect(model.gaps.some((gap) => gap.message.includes("工具来源尚未取得"))).toBe(true);
  });
  it("retains missing parents without fabricating edges and resolves them when later loaded", () => {
    const records = base().filter((row) => row.span_id !== "agent");
    const missing = project(records);
    expect(missing.gaps.some((gap) => gap.message.includes("父调用"))).toBe(true);
    expect(missing.nodes.find((node) => node.item.step.event.span_id === "tool")?.parentId).toBeUndefined();
    const loaded = project([...records, base()[1]]);
    expect(loaded.byId.get(spanKey("trace", "tool")!)?.parentId).toBe(spanKey("trace", "agent"));
  });
  it("does not turn cyclic parents into a graph cycle", () => {
    const model = project([event(1, "a", "b", "SpanFinished"), event(2, "b", "a", "SpanFinished")]);
    expect(model.nodes).toHaveLength(2);
    expect(model.relations).toHaveLength(0);
    expect(model.gaps).toHaveLength(2);
  });
  it("does not create historical data edges when reference metadata is absent", () => {
    const model = project(base().map((row) => row.kind === "ContextSnapshotPrepared" ? { ...row, data: { ...row.data, input_tool_refs: undefined } } : row));
    expect(model.relations.filter((edge) => edge.kind === "input")).toHaveLength(0);
  });
  it("does not attribute an orphan tool message to an unfinished execution", () => {
    const model = project(base().map((row) => row.kind === "ToolFinished" ? { ...row, kind: "ToolStarted", phase: "start", status: "running" } : row));
    expect(model.relations.filter((edge) => edge.kind === "input")).toHaveLength(0);
    expect(model.gaps.some((gap) => gap.message.includes("工具来源尚未取得"))).toBe(true);
  });
  it("keeps untraversed, unloaded and unrecorded layers distinct", () => {
    const model = project([event(1, "accept", undefined, "RuntimeStageFinished", { runtime_layer: "gateway", operation: "gateway.accept", entrypoint: "client" })]);
    expect(layerSummaries(model, true, false).find((layer) => layer.layer === "channel")?.label).toBe("未经过");
    expect(layerSummaries(model, true, true).find((layer) => layer.layer === "agent")?.label).toBe("尚未取得");
    expect(layerSummaries(model, true, false).find((layer) => layer.layer === "agent")?.label).toBe("未记录");
  });
  it("preserves stage order when a layer reappears on the return path", () => {
    const records = [event(0, "prepare", undefined, "RuntimeStageFinished", { runtime_layer: "application", operation: "application.prepare" }), ...base(),
      event(9, "result", undefined, "RuntimeStageFinished", { runtime_layer: "application", operation: "application.result" })];
    expect(project(records).stages.map((stage) => stage.stage?.item.operation)).toEqual(["application.prepare", "agent.execute", "application.result"]);
  });
  it("projects only real runtime stages into the four-layer track and keeps missing ownership separate", () => {
    const model = project([...base(),
      event(7, "missing-tool", undefined, "ToolFinished", { stage_span_id: "missing-stage", tool_call_id: "missing" }),
      event(8, "independent", undefined, "custom", { stage_span_id: undefined, process_kind: "event" })]);
    const projection = executionStageProjection(model);
    expect(projection.stages.map((group) => group.stage.item.operation)).toEqual(["agent.execute"]);
    expect(projection.stages[0].nodes.map((node) => node.item.step.event.span_id)).toEqual(["agent", "model1", "tool", "model2"]);
    expect(projection.unbound).toHaveLength(2);
    expect(projection.unbound.find((group) => group.missingStage)?.nodes[0].item.step.event.span_id).toBe("missing-tool");
    expect(projection.unbound.find((group) => !group.missingStage)?.nodes[0].item.step.event.span_id).toBe("independent");
  });
  it("scopes Agent list and graph nodes to the selected real stage", () => {
    const model = project([...base(),
      event(10, "stage-two", undefined, "RuntimeStageFinished", { operation: "agent.execute", stage_span_id: "stage-two" }),
      event(11, "tool-two", "stage-two", "ToolFinished", { stage_span_id: "stage-two", tool_call_id: "second" })]);
    const scope = agentExecutionScope(model, spanKey("trace", "stage-two")!)!;
    expect(scope.root?.item.step.event.span_id).toBe("stage-two");
    expect(scope.nodes.map((node) => node.item.step.event.span_id)).toEqual(["stage-two", "tool-two"]);
    expect(scope.ids.has(spanKey("trace", "tool")!)).toBe(false);
    const visible = visibleGraph(model, { scope: scope.ids, terminal: true, hasMore: false, collapsed: new Set() });
    expect(visible.nodes.map((node) => node.item.step.event.span_id)).toEqual(["stage-two", "tool-two"]);
    expect(visible.relations.every((relation) => scope.ids.has(relation.source) && scope.ids.has(relation.target))).toBe(true);
  });
  it("allows unbound Agent records in the list without inventing a graph root", () => {
    const model = project([...base(), event(7, "orphan", undefined, "ToolFinished", {
      stage_span_id: "missing-stage", tool_call_id: "orphan",
    })]);
    const scope = agentExecutionScope(model, spanKey("trace", "orphan")!)!;
    expect(scope.root).toBeUndefined();
    expect(scope.nodes.map((node) => node.item.step.event.span_id)).toEqual(["orphan"]);
  });
  it("collapses descendants while retaining the owning Agent and filters with ancestry", () => {
    const model = project([...base(), event(7, "sub", "agent", "SpanFinished", { process_kind: "subagent" }),
      event(8, "sub-model", "sub", "LlmCallFinished", { model: "nested-model" }, { status: "failed" })]);
    const options = { terminal: true, hasMore: false, collapsed: new Set([spanKey("trace", "sub")!]) };
    expect(visibleGraph(model, options).nodes.some((node) => node.item.step.event.span_id === "sub-model")).toBe(false);
    const focused = visibleGraph(model, { ...options, focus: spanKey("trace", "sub") });
    expect(focused.nodes.map((node) => node.item.step.event.span_id)).toEqual(["sub", "sub-model"]);
    const failures = visibleGraph(model, { ...options, collapsed: new Set(), errorsOnly: true });
    expect(failures.nodes.map((node) => node.item.step.event.span_id)).toEqual(["stage", "agent", "sub", "sub-model"]);
  });
  it("projects one thousand repeated calls without dropping or merging identities", () => {
    const model = project([base()[0], base()[1], ...Array.from({ length: 1000 }, (_, index) => event(index + 3, `tool-${index}`, "agent", "ToolFinished", { tool_call_id: `call-${index}`, name: "lookup" }))]);
    expect(model.nodes).toHaveLength(1002);
    expect(new Set(model.nodes.map((node) => node.id)).size).toBe(1002);
    expect(model.relations).toHaveLength(1001);
  });
});
