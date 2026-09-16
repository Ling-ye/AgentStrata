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
  const body = { scope: "all" as const, model: "test-model", reasoning_effort: "xhigh", max_attempts: 3, budget: { mode: "fixed_groups" as const, count: 1 }, request_id: "request" };
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

it("keeps cancellation and budgets distinct from partial improvements", () => {
  const governance_summary = { found: 4, fixed: 2, needs_decision: 1, remaining: 1,
    coverage: "partial" as const, completed_batches: 2, total_batches: 4 };
  expect(healthStatus({ status: "fixed", governance_summary })).toBe("已修复部分");
  expect(healthStatus({ status: "cancelled", governance_summary })).toBe("已取消");
  expect(healthStatus({ status: "blocked", stop_reason: "budget_exhausted", governance_summary })).toBe("已达总时限");
  expect(healthStatus({ status: "blocked", stop_reason: "execution_failed", governance_summary })).toBe("执行受阻");
  expect(healthStatus({ status: "blocked", stop_reason: "step_timeout", governance_summary })).toBe("单次执行超时");
  expect(healthStatus({ status: "fixed", stop_reason: "fix_limit_reached", governance_summary })).toBe("已达修复数量目标");
});

it("does not invalidate archived acceptance using new snapshot rules", () => {
  expect(healthStatus({ status: "fixed", candidate_available: null, governance_summary:
    { found: 1, fixed: 1, needs_decision: 0, remaining: 0, coverage: "unknown", completed_batches: 0, total_batches: 0 }
  })).toBe("历史验收通过");
});

it("keeps accepted findings tied to their checkpoint while the next candidate changes", () => {
  const task = { candidate_available: false, checkpoint_available: true,
    checkpoint: { number: 1 }, governance: { resolved_ids: ["finding"] } } as HealthTask;
  expect(findingStatus({ id: "finding" } as Finding, task)).toBe("已保存在检查点");
  task.checkpoint_available = false;
  expect(findingStatus({ id: "finding" } as Finding, task)).toBe("检查点不可读取");
});

it("discovery threshold does not imply successful repair", () => {
  const base = { stop_reason: "discovery_limit_reached", governance_summary: {
    found: 3, fixed: 1, needs_decision: 0, remaining: 2, discovered_groups: 3, selected_groups: 1,
    accepted_groups: 1, completed_batches: 1, total_batches: 10, coverage: "partial" as const } };
  expect(healthStatus({ ...base, status: "fixed" })).toBe("已修复选中问题");
  expect(healthStatus({ ...base, status: "failed" })).toBe("选中问题未通过验收");
  expect(healthStatus({ ...base, status: "blocked" })).toBe("选中问题待判断");
  expect(healthStatus({ ...base, status: "fixed", governance_summary: { ...base.governance_summary, selected_groups: 2 } })).toBe("已修复部分选中问题");
});
