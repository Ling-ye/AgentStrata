import { afterEach, describe, expect, it, vi } from "vitest";
import { commandSourceLabel, descendants, flowApi, flowChildren, initialExpansion, visibleCommands, type FlowStep, type RepairFlow } from "./flowModel";

afterEach(() => vi.unstubAllGlobals());
const step = (id: string, parent: string, phase: string, started_at?: number): FlowStep => ({ id, parent_id: parent, title: id, phase, status: "completed", input_summary: "source", conclusion: "result", started_at });
const flow: RepairFlow = { task_id: "task-a", default_step_id: "review", groups: [
  { id: "attempts", parent_id: null, title: "修复尝试" }, { id: "attempt-1", parent_id: "attempts", title: "第 1 轮" },
  { id: "prepare-2", parent_id: "attempt-1", title: "第 2 版", started_at: 20 },
], steps: [step("code", "attempt-1", "coding", 10), step("draft", "prepare-2", "prepare", 21), step("review", "attempt-1", "review", 30)] };

describe("repair flow hierarchy", () => {
  it("expands exactly the current or final step and its ancestors", () => {
    expect(initialExpansion(flow)).toEqual({ review: true, "attempt-1": true, attempts: true });
    expect(initialExpansion({ ...flow, default_step_id: "draft" })).toEqual({ draft: true, "prepare-2": true, "attempt-1": true, attempts: true });
  });
  it("places preparation revisions between the actual surrounding steps", () => {
    expect(flowChildren(flow, "attempt-1").map(c => c.id)).toEqual(["code", "prepare-2", "review"]);
    expect(descendants(flow, "attempts").map(s => s.id)).toEqual(["code", "draft", "review"]);
  });
  it("orders historical steps by business category when timestamps were not recorded", () => {
    const historical = { ...flow, groups: [], steps: [step("check", "attempt-1", "verify-1"), step("coding", "attempt-1", "coding")] };
    expect(flowChildren(historical, "attempt-1").map(s => s.id)).toEqual(["coding", "check"]);
  });
  it("keeps null exit status distinct from both success and failure", () => {
    const events = [0, 1, null].map((exit_code, i) => ({ id: String(i), command: "test", exit_code, truncated: false }));
    expect(visibleCommands(events, true).map(e => e.exit_code)).toEqual([1]);
    expect(visibleCommands(events, false)).toHaveLength(3);
  });
  it("labels preparation review separately from repair review", () => {
    expect(commandSourceLabel({ id: "prepare-review-2", kind: "review", number: 2, current: false })).toBe("第 2 版 · 方案审核");
    expect(commandSourceLabel({ id: "review-2", kind: "review", number: 2, current: false })).toBe("第 2 轮 · 修复审核");
  });
  it("binds reads and cancellation to the selected task and source", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetch);
    const signal = new AbortController().signal;
    await flowApi.step("task a", "step b", signal);
    expect(fetch.mock.calls[0]).toEqual(["/api/harness/tasks/task%20a/flow/steps/step%20b", { signal, cache: "no-store" }]);
    await flowApi.commands("task a", "prepare-review-2", "cursor+", signal);
    expect(fetch.mock.calls[1][0]).toBe("/api/harness/tasks/task%20a/commands?source_id=prepare-review-2&cursor=cursor%2B");
  });
});
