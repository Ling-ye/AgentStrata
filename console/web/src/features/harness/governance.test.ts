import { afterEach, expect, it, vi } from "vitest";
import { governanceApi } from "./governance";
import { deliveryActive, repairStatusLabel, sourceLabel, type RepairTask } from "./api";

afterEach(() => vi.unstubAllGlobals());

it("loads worker model choices without caching", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ([
    { model: "gpt-6-sol", reasoning_efforts: ["medium", "high"] },
  ]) });
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  expect(await governanceApi.models(signal)).toEqual([
    { model: "gpt-6-sol", reasoning_efforts: ["medium", "high"] },
  ]);
  expect(fetch.mock.calls[0]).toEqual(["/api/harness/code-health/models", { signal, cache: "no-store" }]);
});

it("starts a sequential run with a mutually exclusive stopping condition", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ run_id: "gc-example" }) });
  vi.stubGlobal("fetch", fetch);
  const body = { request_id: "stable-gc", model: "fixture", reasoning_effort: "medium", max_attempts: 3,
    stop_condition: { mode: "findings" as const, count: 2 } };
  await governanceApi.start(body);
  expect(fetch.mock.calls[0][0]).toBe("/api/harness/code-health/runs");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body);
});

it("saves an explicit optional schedule without task side effects", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ enabled: false }) });
  vi.stubGlobal("fetch", fetch);
  const body = { enabled: false, interval_hours: 24, options: null };
  await governanceApi.saveSchedule(body);
  expect(fetch.mock.calls[0][0]).toBe("/api/harness/code-health/schedule");
  expect(fetch.mock.calls[0][1].method).toBe("PUT");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body);
});

it("loads governance evidence independently and without caching", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ state: "pending", report: null }) });
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await governanceApi.report("repair gc", signal);
  expect(fetch.mock.calls[0]).toEqual(["/api/harness/tasks/repair%20gc/governance", { signal, cache: "no-store" }]);
});

it("labels green GC outcomes distinctly and stops polling after no changes", () => {
  const task = { source: { kind: "code_health" }, status: "fixed", governance_summary: { topic: "消除重复" } } as RepairTask;
  expect(sourceLabel(task)).toBe("代码熵回收 · 消除重复");
  expect(repairStatusLabel(task)).toBe("熵回收验收通过");
  expect(repairStatusLabel({ ...task, status: "no_changes" })).toBe("无需改动");
  expect(deliveryActive({ ...task, status: "no_changes" })).toBe(false);
});
