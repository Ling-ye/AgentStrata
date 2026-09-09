import { describe, expect, it } from "vitest";
import { buildRunTree, buildRunView, stepIsOpen, stepState, stepDuration, stepRuntimeLayer, observationQuery, related, duration, runDuration, bodyState } from "./workbenchModel";
import type { GatewayObservation, GatewayRun, GatewayRunDetail } from "./model";

const event = (seq: number, props: Partial<GatewayObservation>): GatewayObservation => ({ seq, kind: "ToolStarted", created_at: seq, ...props, data: { flow_version: 1, runtime_layer: "agent", ...props.data } });
describe("run observation workbench", () => {
  it("links model calls to the recorded actor and retains the key when its finish arrives", () => {
    const actor = event(1, { kind: "RuntimeStageStarted", trace_id: "run", span_id: "host:actor", phase: "start", data: { operation: "agent.execute" } });
    const model = event(2, { kind: "LlmCallStarted", trace_id: "run", span_id: "model", parent_span_id: "host:actor", phase: "start" });
    const initial = buildRunView([actor, model]);
    const refreshed = buildRunView([actor, model,
      event(3, { kind: "LlmCallFinished", trace_id: "run", span_id: "model", parent_span_id: "host:actor", phase: "finish", status: "succeeded" }),
      event(4, { kind: "RuntimeStageFinished", trace_id: "run", span_id: "host:actor", phase: "finish", status: "succeeded", data: { operation: "agent.execute" } }),
    ]);
    expect(refreshed.steps.map((step) => [step.key, step.depth, step.missingParent])).toEqual(
      initial.steps.map((step) => [step.key, step.depth, false]));
    expect(refreshed.steps[1].depth).toBe(1);
  });
  it("pairs Channel dispatch and return while updating from the exact outbound receipt before event refresh", () => {
    const start = event(1, { kind: "RuntimeStageStarted", trace_id: "run", span_id: "delivery-a", phase: "start", status: "running", data: { operation: "channel.deliver", runtime_layer: "channel", outbound_id: "a" } });
    const receipts: GatewayRunDetail["receipts"] = [{ receipt_id: "ack", outbound_id: "a", stage: "provider_acknowledged", observed_at: 2, error_code: null }];
    const active = buildRunView([start]).steps[0];
    expect(stepState(active, false).status).toBe("running");
    const received = buildRunView([start], { receipts, outbox: [] }).steps[0];
    expect(received.key).toBe(active.key);
    expect(stepState(received, true)).toMatchObject({ status: "provider_acknowledged", label: "Provider 已确认", incomplete: false });
    const finished = buildRunView([start, event(2, { kind: "RuntimeStageFinished", trace_id: "run", span_id: "delivery-a", phase: "finish", status: "provider_acknowledged", data: { operation: "channel.deliver", runtime_layer: "channel", outbound_id: "a" } })], { receipts, outbox: [] });
    expect(finished.steps).toHaveLength(1);
    expect(finished.steps[0].key).toBe(active.key);
  });
  it("never promotes unrelated delivery or task completion to a successful dispatch", () => {
    const start = event(1, { kind: "RuntimeStageStarted", trace_id: "run", span_id: "delivery-a", phase: "start", status: "running", data: { operation: "channel.deliver", runtime_layer: "channel", outbound_id: "a" } });
    const delivery = { receipts: [{ receipt_id: "other", outbound_id: "b", stage: "provider_acknowledged", observed_at: 2, error_code: null }], outbox: [] };
    const [step] = buildRunView([start], delivery).steps;
    expect(stepState(step, true).label).toBe("结束未记录");
    expect(stepState(step, false).status).toBe("running");
    const incomplete = buildRunView([event(1, { kind: "RuntimeStageStarted", status: "running" })], delivery);
    expect(stepState(incomplete.steps[0], true).label).toBe("结束未记录");
  });
  it.each(["failed", "delivery_unknown", "pending"])("uses the authoritative %s outbound state without borrowing another reply's acknowledgement", (status) => {
    const [step] = buildRunView([event(1, { kind: "RuntimeStageStarted", phase: "start", status: "running", data: { operation: "channel.deliver", runtime_layer: "channel", outbound_id: "a" } })], {
      receipts: [{ receipt_id: "ack", outbound_id: "b", stage: "provider_acknowledged", observed_at: 3, error_code: null }],
      outbox: [{ outbound_id: "a", state: status, created_at: 1, updated_at: 2, error_code: "fixture-code" }],
    }).steps;
    expect(stepState(step, true).status).toBe(status);
    expect(step.delivery?.errorCode).toBe("fixture-code");
  });
  it("pairs only identical trace and span IDs and preserves parallel children", () => {
    const tree = buildRunTree([
      event(1, { trace_id: "one", span_id: "root", phase: "start" }),
      event(2, { trace_id: "one", span_id: "a", parent_span_id: "root", phase: "start" }),
      event(3, { trace_id: "one", span_id: "b", parent_span_id: "root", phase: "start" }),
      event(4, { trace_id: "one", span_id: "a", parent_span_id: "root", phase: "finish", elapsed_ms: 20 }),
      event(5, { trace_id: "two", span_id: "a", phase: "finish" }),
    ]);
    expect(tree).toHaveLength(2);
    expect(tree[0].children).toHaveLength(2);
    expect(tree[0].children[0].event.seq).toBe(4);
    expect(tree[0].children[1].finish).toBeUndefined();
    expect(tree[1].start).toBeUndefined();
  });
  it("keeps missing parents and cyclic relations visible without inventing ancestry", () => {
    const tree = buildRunTree([
      event(1, { trace_id: "t", span_id: "a", parent_span_id: "b", phase: "start" }),
      event(2, { trace_id: "t", span_id: "b", parent_span_id: "a", phase: "start" }),
      event(3, { trace_id: "t", span_id: "c", parent_span_id: "absent", phase: "finish" }),
    ]);
    expect(tree).toHaveLength(3);
    expect(tree.every((node) => node.missingParent && node.children.length === 0)).toBe(true);
  });
  it("uses explicit component bindings and never guesses by tool name", () => {
    const call = event(1, { entity_id: "tool:lookup", name: "lookup", refs: ["mcp:search"] });
    expect(related(call, "mcp:search")).toBe(true);
    expect(related(call, "pack:lookup")).toBe(false);
    expect(related(call, "")).toBe(true);
  });
  it("encodes server-side history filters without limiting the searchable history", () => {
    const params = new URLSearchParams(observationQuery({ search: "run & older", page: 4, component: "mcp:search", min_ms: 0 }).slice(1));
    expect(params.get("search")).toBe("run & older");
    expect(params.get("page")).toBe("4");
    expect(params.get("min_ms")).toBe("0");
    expect(params.has("limit")).toBe(false);
  });
  it("keeps unknown duration and body gaps distinct from zero and complete records", () => {
    expect(duration(null)).toBe("未记录");
    expect(duration(0)).toBe("0 ms");
    expect(runDuration({ started_at: 10, finished_at: 12 } as GatewayRun, 20)).toBe(2000);
    expect(new Set(["available", "expired", "not_recorded", "truncated", "capture_failed"].map(bodyState)).size).toBe(5);
  });
  it("keeps repeated tool calls separate and attaches only explicitly bound permission and log records", () => {
    const view = buildRunView([
      event(1, { trace_id: "t", span_id: "a", phase: "start", entity_id: "tool:lookup" }),
      event(2, { trace_id: "t", span_id: "b", phase: "start", entity_id: "tool:lookup" }),
      event(3, { kind: "tool_authorization", trace_id: "t", span_id: "b", entity_id: "tool:lookup", data: { phase: "execution", allowed: false } }),
      event(4, { kind: "tool_authorization", entity_id: "tool:lookup", data: { phase: "visibility" } }),
      event(5, { kind: "log", trace_id: "t", span_id: "a" }),
      event(6, { kind: "log", entity_id: "tool:lookup" }),
      event(7, { trace_id: "t", span_id: "a", phase: "finish", entity_id: "tool:lookup" }),
    ]);
    expect(view.steps).toHaveLength(2);
    expect(view.steps[0].permissions).toEqual([]);
    expect(view.steps[0].logs.map((item) => item.seq)).toEqual([5]);
    expect(view.steps[1].permissions.map((item) => item.seq)).toEqual([3]);
    expect(view.permissions.map((item) => item.seq)).toEqual([4]);
    expect(view.logs.map((item) => item.seq)).toEqual([6]);
  });
  it("puts context on its own model call and keeps orphan snapshots and task states accessible", () => {
    const view = buildRunView([
      event(1, { kind: "ContextSnapshotPrepared", data: { snapshot_id: "one" } }),
      event(2, { kind: "ContextSnapshotPrepared", data: { snapshot_id: "orphan" } }),
      event(3, { kind: "LlmCallStarted", trace_id: "t", span_id: "model", phase: "start", data: { context_snapshot_id: "one" } }),
      event(4, { kind: "LlmCallFinished", trace_id: "t", span_id: "model", phase: "finish" }),
      event(5, { kind: "run_state", status: "completed" }),
    ]);
    expect(view.steps).toHaveLength(2);
    expect(view.steps[0].event.seq).toBe(2);
    expect(view.steps[1].contexts.map((item) => item.seq)).toEqual([1]);
    expect(view.states.map((item) => item.seq)).toEqual([5]);
  });
  it("does not guess context identity when the snapshot ID has multiple records", () => {
    const view = buildRunView([
      event(1, { kind: "ContextSnapshotPrepared", data: { snapshot_id: "duplicate" } }),
      event(2, { kind: "ContextSnapshotPrepared", data: { snapshot_id: "duplicate" } }),
      event(3, { kind: "LlmCallStarted", trace_id: "t", span_id: "model", phase: "start", data: { context_snapshot_id: "duplicate" } }),
    ]);
    expect(view.steps).toHaveLength(3);
    expect(view.steps.every((step) => !step.contexts.length)).toBe(true);
  });
  it("preserves step keys across page updates and inherits parent identity from the start record", () => {
    const start = event(1, { trace_id: "t", span_id: "root", phase: "start", status: "running" });
    const child = event(2, { trace_id: "t", span_id: "child", parent_span_id: "root", phase: "start" });
    const first = buildRunView([start, child]);
    const later = buildRunView([start, child, child, event(3, { trace_id: "t", span_id: "child", phase: "finish" })]);
    expect(later.steps).toHaveLength(2);
    expect(later.steps.map((step) => step.key)).toEqual(first.steps.map((step) => step.key));
    expect(later.steps[1].depth).toBe(1);
    expect(later.steps[1].start?.seq).toBe(2);
    expect(later.steps[1].finish?.seq).toBe(3);
  });
  it("does not merge missing trace IDs or delimiter-colliding identifiers", () => {
    const view = buildRunView([
      event(1, { span_id: "a", phase: "start" }), event(2, { span_id: "a", phase: "finish" }),
      event(3, { trace_id: "a:b", span_id: "c", phase: "start" }),
      event(4, { trace_id: "a", span_id: "b:c", phase: "finish" }),
    ]);
    expect(view.steps).toHaveLength(4);
  });
  it("preserves explicit collapse through failure and polling while opening new active steps", () => {
    const [active] = buildRunView([event(1, { status: "running" })]).steps;
    expect(stepIsOpen(active, {}, false)).toBe(true);
    expect(stepIsOpen(active, {}, true)).toBe(false);
    expect(stepIsOpen(active, { [active.key]: false }, false)).toBe(false);
    const failed = { ...active, event: { ...active.event, status: "failed" } };
    expect(stepIsOpen(failed, {}, true)).toBe(true);
    expect(stepIsOpen(failed, { [active.key]: false }, true)).toBe(false);
  });
});

const stage = (seq: number, span: string, layer: string, operation: string, phase = "start", extra: Partial<GatewayObservation> = {}): GatewayObservation =>
  event(seq, { kind: phase === "start" ? "RuntimeStageStarted" : "RuntimeStageFinished", phase, trace_id: "run", span_id: span,
    status: phase === "start" ? "running" : "succeeded", body_ref: `body-${seq}`,
    ...extra, data: { flow_version: 1, runtime_layer: layer, operation, ...extra.data } });

describe("four-layer task flow", () => {
  it("distinguishes a terminal stage whose finish is on an unread page from a missing finish", () => {
    const view = buildRunView([stage(1, "host:actor", "agent", "agent.execute")]);
    expect(stepState(view.steps[0], true, true)).toMatchObject({ incomplete: true, status: "unknown", label: "结束记录尚未取得" });
    expect(stepState(view.steps[0], true, false)).toMatchObject({ incomplete: true, status: "unknown", label: "结束未记录" });
  });
  it("keeps repeated layer visits in their recorded order with input and output paired once", () => {
    const events = [
      stage(1, "receive", "channel", "channel.receive"), stage(2, "receive", "channel", "channel.receive", "finish"),
      stage(3, "accept", "gateway", "gateway.accept"), stage(4, "accept", "gateway", "gateway.accept", "finish"),
      stage(5, "prepare", "application", "application.prepare"), stage(6, "prepare", "application", "application.prepare", "finish"),
      stage(7, "host:actor", "agent", "agent.execute"), stage(8, "host:actor", "agent", "agent.execute", "finish"),
      stage(9, "dispatch", "gateway", "gateway.dispatch"), stage(10, "dispatch", "gateway", "gateway.dispatch", "finish"),
      stage(11, "deliver", "channel", "channel.deliver"), stage(12, "deliver", "channel", "channel.deliver", "finish"),
      stage(13, "exchange", "application", "application.exchange"), stage(14, "exchange", "application", "application.exchange", "finish"),
    ];
    const view = buildRunView(events);
    expect(view.flow.map((item) => [item.kind, item.layer])).toEqual([
      ["stage", "channel"], ["stage", "gateway"], ["stage", "application"], ["stage", "agent"],
      ["stage", "gateway"], ["stage", "channel"], ["stage", "application"],
    ]);
    expect(view.flow[0].step.start?.body_ref).toBe("body-1");
    expect(view.flow[0].step.finish?.body_ref).toBe("body-2");
  });

  it("places simultaneous same-name calls and their nested children under only their real stage", () => {
    const call = (seq: number, span: string, parent: string, phase = "start") => event(seq, {
      kind: phase === "start" ? "ToolStarted" : "ToolFinished", trace_id: "run", span_id: span, parent_span_id: parent,
      phase, entity_id: "tool:lookup", name: "lookup", body_ref: `call-${span}-${phase}`,
      data: { flow_version: 1, runtime_layer: "agent", stage_span_id: "host:actor" },
    });
    const view = buildRunView([
      stage(1, "host:actor", "agent", "agent.execute"), call(2, "a", "host:actor"), call(3, "b", "host:actor"),
      call(4, "nested", "a"), call(5, "a", "host:actor", "finish"), call(6, "b", "host:actor", "finish"),
      stage(7, "host:actor", "agent", "agent.execute", "finish"),
      event(8, { kind: "tool_authorization", trace_id: "run", span_id: "b", entity_id: "tool:lookup", data: { allowed: false, phase: "execution" } }),
      event(9, { kind: "log", trace_id: "run", span_id: "nested" }), event(10, { kind: "log", data: { logger: "lookup" } }),
    ]);
    expect(view.flow.map((item) => [item.step.event.span_id, item.step.depth])).toEqual([["host:actor", 0], ["a", 0], ["nested", 1], ["b", 0]]);
    expect(view.flow[1].step.finish?.body_ref).toBe("call-a-finish");
    expect(view.flow[3].step.permissions.map((entry) => entry.seq)).toEqual([8]);
    expect(view.flow[2].step.logs.map((entry) => entry.seq)).toEqual([9]);
    expect(view.logs.map((entry) => entry.seq)).toEqual([10]);
  });

  it("ignores unversioned history instead of adapting it into current steps", () => {
    const old = [
      event(2, { kind: "actor_execution", trace_id: "run", span_id: "host:actor", phase: "start", data: { flow_version: undefined } }),
      event(3, { kind: "actor_returned", trace_id: "run", span_id: "host:actor", phase: "finish", data: { flow_version: undefined } }),
      event(6, { kind: "response_dispatch", trace_id: "run", span_id: "old-delivery", phase: "start", data: { flow_version: undefined, outbound_id: "one" } }),
      event(7, { kind: "tool_authorization", status: "failed", data: { flow_version: undefined, phase: "visibility" } }),
    ];
    expect(buildRunView(old)).toMatchObject({ steps: [], flow: [], permissions: [] });
    const view = buildRunView([
      stage(1, "host:actor", "agent", "agent.execute"), ...old,
      stage(4, "host:actor", "agent", "agent.execute", "finish"),
      stage(5, "delivery", "channel", "channel.deliver", "start", { data: { outbound_id: "one" } }),
    ]);
    expect(view.steps.map((step) => step.event.seq)).toEqual([4, 5]);
    expect(view.flow.every((item) => item.kind === "stage")).toBe(true);
  });

  it("retains call keys when a late stage and parent arrive on another page", () => {
    const model = event(2, { kind: "LlmCallStarted", trace_id: "run", span_id: "model", parent_span_id: "host:actor", phase: "start",
      data: { flow_version: 1, runtime_layer: "agent", stage_span_id: "host:actor", context_snapshot_id: "snapshot" } });
    const first = buildRunView([model]);
    const later = buildRunView([stage(1, "host:actor", "agent", "agent.execute"), model, model,
      event(3, { kind: "ContextSnapshotPrepared", trace_id: "run", data: { snapshot_id: "snapshot" }, body_ref: "context-body" }),
      event(4, { kind: "LlmCallFinished", trace_id: "run", span_id: "model", phase: "finish", body_ref: "visible-response" }),
      stage(5, "host:actor", "agent", "agent.execute", "finish")]);
    expect(first.flow[0].missingStage).toBe(true);
    expect(later.flow[1].step.key).toBe(first.flow[0].step.key);
    expect(later.flow[1].missingStage).toBe(false);
    expect(later.flow[1].step.contexts.map((context) => context.body_ref)).toEqual(["context-body"]);
    expect(later.flow[1].step.finish?.body_ref).toBe("visible-response");
    expect(stepIsOpen(later.flow[1].step, { [first.flow[0].step.key]: false }, true)).toBe(false);
  });

  it("never substitutes a same-named stage from another trace or a configuration layer", () => {
    const view = buildRunView([
      stage(1, "host:actor", "agent", "agent.execute"),
      event(2, { kind: "ToolStarted", trace_id: "other", span_id: "tool", phase: "start", layer: "capability",
        data: { flow_version: 1, runtime_layer: "agent", stage_span_id: "host:actor" } }),
      event(3, { kind: "custom", layer: "authorization", status: "succeeded", data: { runtime_layer: undefined } }),
    ]);
    expect(view.flow[1].missingStage).toBe(true);
    expect(view.flow[2].layer).toBeUndefined();
    const old = buildRunView([event(1, { kind: "LlmCallStarted", layer: "capability", phase: "start" })]);
    expect(old.flow[0].kind).toBe("step");
    expect(old.flow[0].layer).toBe("agent");
    expect(old.flow[0].step.start?.body_ref).toBeUndefined();
  });

  it("uses the start owner after a finish changes its direction and preserves unknown delivery", () => {
    const started = stage(1, "delivery", "channel", "channel.deliver", "start", { data: { source: "gateway", target: "channel", outbound_id: "one" } });
    const ended = stage(2, "delivery", "channel", "channel.deliver", "finish", { data: { source: "channel", target: "gateway", outbound_id: "one" } });
    const view = buildRunView([started, ended], { outbox: [{ outbound_id: "one", state: "delivery_unknown", created_at: 1, updated_at: 2, error_code: null }],
      receipts: [{ receipt_id: "other", outbound_id: "two", observed_at: 3, stage: "provider_acknowledged", error_code: null }] });
    expect(view.flow[0].layer).toBe("channel");
    expect(stepRuntimeLayer(view.steps[0])).toBe("channel");
    expect(stepState(view.steps[0], true)).toMatchObject({ status: "delivery_unknown", label: "交付结果未知" });
  });

  it("does not manufacture durations for retrospective facts or add parallel call durations", () => {
    const facts = buildRunView([
      stage(1, "receive", "channel", "channel.receive", "start", { data: { duration_recorded: false } }),
      stage(4, "receive", "channel", "channel.receive", "finish", { data: { duration_recorded: false } }),
      stage(5, "prepare", "application", "application.prepare"), stage(8, "prepare", "application", "application.prepare", "finish", { elapsed_ms: 25 }),
    ]);
    expect(stepDuration(facts.steps[0], true)).toBeNull();
    expect(stepDuration(facts.steps[1], true)).toBe(25);
    expect(runDuration({ started_at: 1, finished_at: 4 } as GatewayRun)).toBe(3000);
  });
});
