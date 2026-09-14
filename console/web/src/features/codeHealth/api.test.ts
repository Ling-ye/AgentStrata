import { afterEach, expect, it, vi } from "vitest";
import { healthApi, healthStatus, findingStatus, type Finding, type HealthTask } from "./api";

afterEach(() => vi.unstubAllGlobals());

it("uses the shared host with an explicit governance history filter", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ tasks: [], total: 0 }) });
  vi.stubGlobal("fetch", fetch);
  await healthApi.history(2, "repair sample", "blocked");
  expect(fetch.mock.calls[0][0]).toBe("/api/harness/tasks?page=2&search=repair+sample&status=blocked&kind=code_health");
});

it("submits source scope and execution parameters without Git authority", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ task_id: "example" }) });
  vi.stubGlobal("fetch", fetch);
  const body = { scope: "all" as const, model: "test-model", reasoning_effort: "medium", max_attempts: 3, timeout_seconds: 7200, request_id: "request" };
  await healthApi.start(body);
  expect(fetch.mock.calls[0][0]).toBe("/api/harness/code-health/tasks");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body);
});

it("distinguishes historical acceptance from an available candidate", () => {
  expect(healthStatus({ status: "fixed", candidate_available: false })).toContain("候选已变化");
  expect(healthStatus({ status: "fixed", candidate_available: true })).toBe("已验证，待人工提交");
  expect(healthStatus({ status: "not_reproduced" })).toBe("扫描完成");
  const task = { candidate_available: false, governance: { resolved_ids: ["finding"] } } as HealthTask;
  expect(findingStatus({ id: "finding" } as Finding, task)).toBe("验收后候选已变化");
});

it("only requests a registered check reference and escapes task identity", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ text: "passed" }) });
  vi.stubGlobal("fetch", fetch);
  await healthApi.log("repair example", "checks/one/log.txt");
  expect(fetch.mock.calls[0][0]).toBe("/api/harness/tasks/repair%20example/check-log?reference=checks%2Fone%2Flog.txt");
});
