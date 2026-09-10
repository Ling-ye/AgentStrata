import { describe, expect, it } from "vitest";
import { deltaContent } from "./AgentStreamContent";
import { mergeObservationEvents } from "./useObservationStream";
import { buildRunView } from "./workbenchModel";
import type { GatewayObservation } from "./model";

const event = (seq: number, revision = seq): GatewayObservation => ({ seq, kind: "AgentContentDelta", created_at: seq,
  phase: "update", trace_id: "run", span_id: "message", status: "running", body_ref: String(seq),
  data: { flow_version: 1, runtime_layer: "agent", process_kind: "message", revision } });
const body = (delta: string, section = 0) => ({ body_id: "body", state: "available", payload: { delta, section } });

describe("durable Agent content", () => {
  it("merges replayed events once and keeps one item through completion", () => {
    const merged = mergeObservationEvents([event(1), event(2)], [event(2), event(3)]);
    expect(merged.map((item) => item.seq)).toEqual([1, 2, 3]);
    const final = { ...event(4), kind: "AgentMessageObserved", phase: "finish", status: "succeeded" };
    const view = buildRunView([...merged, final, event(5)]);
    expect(view.steps).toHaveLength(1);
    expect(view.steps[0].event).toBe(final);
    expect(view.steps[0].deltas).toHaveLength(4);
  });
  it("joins ordered summary sections without repeating revisions", () => {
    const content = deltaContent([event(1), event(2), event(3), event(4, 2)],
      [body("先"), body("检查"), body("再处理", 1), body("检查")]);
    expect(content).toEqual({ text: "先检查\n\n再处理", gap: false, truncated: false });
  });
  it("marks missing and truncated bodies without synthesizing text", () => {
    const content = deltaContent([event(1), { ...event(2), body_state: "truncated" }],
      [undefined, { ...body("已取得"), state: "truncated" }]);
    expect(content).toEqual({ text: "已取得", gap: true, truncated: true });
  });
  it("keeps quota exhaustion explicit when no body file could be written", () => {
    expect(deltaContent([{ ...event(1), body_ref: undefined, body_state: "truncated" }], [undefined]))
      .toEqual({ text: "", gap: true, truncated: true });
  });
});
