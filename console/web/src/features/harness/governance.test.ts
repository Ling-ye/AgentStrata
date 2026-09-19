import { afterEach, expect, it, vi } from "vitest";
import { governanceApi } from "./governance";
import { deliveryActive, harnessApi, repairStatusLabel, sourceLabel, type RepairTask } from "./api";

afterEach(() => vi.unstubAllGlobals());

it("starts GC through the common task API without a fabricated case or robot", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ task_id: "repair-gc" }) });
  vi.stubGlobal("fetch", fetch);
  await harnessApi.start({ source_kind: "code_health", request_id: "stable-gc", model: "fixture",
    reasoning_effort: "medium", max_attempts: 3, timeout_seconds: 3600, feedback: { repair_hint: "inspect duplication" } });
  expect(fetch.mock.calls[0][0]).toBe("/api/harness/tasks");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
    source_kind: "code_health", request_id: "stable-gc", model: "fixture", reasoning_effort: "medium",
    max_attempts: 3, timeout_seconds: 3600, feedback: { repair_hint: "inspect duplication" },
  });
});

it("saves an explicit optional schedule without task side effects", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ enabled: false }) });
  vi.stubGlobal("fetch", fetch);
  const body = { enabled: false, interval_hours: 24, options: null, repair_hint: "" };
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
  expect(sourceLabel(task)).toBe("代码治理 · 消除重复");
  expect(repairStatusLabel(task)).toBe("治理验收通过");
  expect(repairStatusLabel({ ...task, status: "no_changes" })).toBe("无需改动");
  expect(deliveryActive({ ...task, status: "no_changes" })).toBe(false);
});
