import { describe, expect, it } from "vitest";
import { buildRunTree, buildRunView, stepIsOpen, stepState, observationQuery, related, duration, runDuration, bodyState } from "./workbenchModel";
import type { GatewayObservation, GatewayRun, GatewayRunDetail } from "./model";

const event = (seq: number, props: Partial<GatewayObservation>): GatewayObservation => ({ seq, kind: "ToolStarted", created_at: seq, ...props });
describe("run observation workbench", () => {
  it("links model calls to the recorded actor and retains the key when its finish arrives", () => {
    const actor = event(1, { kind: "actor_execution", trace_id: "run", span_id: "host:actor", phase: "start" });
    const model = event(2, { kind: "LlmCallStarted", trace_id: "run", span_id: "model", parent_span_id: "host:actor", phase: "start" });
    const initial = buildRunView([actor, model]);
    const refreshed = buildRunView([actor, model,
      event(3, { kind: "LlmCallFinished", trace_id: "run", span_id: "model", parent_span_id: "host:actor", phase: "finish", status: "succeeded" }),
      event(4, { kind: "actor_returned", trace_id: "run", span_id: "host:actor", phase: "finish", status: "succeeded" }),
    ]);
    expect(refreshed.steps.map((step) => [step.key, step.depth, step.missingParent])).toEqual(
      initial.steps.map((step) => [step.key, step.depth, false]));
    expect(refreshed.steps[1].depth).toBe(1);
  });
  it("pairs Channel dispatch and return while updating from the exact outbound receipt before event refresh", () => {
    const start = event(1, { kind: "response_dispatch", trace_id: "run", span_id: "delivery-a", phase: "start", status: "running", data: { outbound_id: "a" } });
    const receipts: GatewayRunDetail["receipts"] = [{ receipt_id: "ack", outbound_id: "a", stage: "provider_acknowledged", observed_at: 2, error_code: null }];
    const active = buildRunView([start]).steps[0];
    expect(stepState(active, false).status).toBe("running");
    const received = buildRunView([start], { receipts, outbox: [] }).steps[0];
    expect(received.key).toBe(active.key);
    expect(stepState(received, true)).toMatchObject({ status: "provider_acknowledged", label: "Provider 已确认", incomplete: false });
    const finished = buildRunView([start, event(2, { kind: "channel_returned", trace_id: "run", span_id: "delivery-a", phase: "finish", status: "provider_acknowledged", data: { outbound_id: "a" } })], { receipts, outbox: [] });
    expect(finished.steps).toHaveLength(1);
    expect(finished.steps[0].key).toBe(active.key);
  });
  it("never promotes unrelated delivery or task completion to a successful dispatch", () => {
    const start = event(1, { kind: "response_dispatch", trace_id: "run", span_id: "delivery-a", phase: "start", status: "running", data: { outbound_id: "a" } });
    const delivery = { receipts: [{ receipt_id: "other", outbound_id: "b", stage: "provider_acknowledged", observed_at: 2, error_code: null }], outbox: [] };
    const [step] = buildRunView([start], delivery).steps;
    expect(stepState(step, true).label).toBe("结束未记录");
    expect(stepState(step, false).status).toBe("running");
    const [historical] = buildRunView([event(1, { kind: "response_dispatch", status: "running" })], delivery).steps;
    expect(stepState(historical, true).label).toBe("结束未记录");
  });
  it.each(["failed", "delivery_unknown", "pending"])("uses the authoritative %s outbound state without borrowing another reply's acknowledgement", (status) => {
    const [step] = buildRunView([event(1, { kind: "response_dispatch", phase: "start", status: "running", data: { outbound_id: "a" } })], {
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
