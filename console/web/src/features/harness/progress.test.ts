import { describe, expect, it } from "vitest";
import { heartbeatStatus, progressSourceLabel, repairRoundLabel } from "./progress";
import type { RepairTask } from "./api";

const task = { status: "running", stage: "coding", heartbeat_at: 100, current_attempt: 2 } as RepairTask;

describe("repair progress facts", () => {
  it("distinguishes missing heartbeat, elapsed time and stale active heartbeat", () => {
    expect(heartbeatStatus({ ...task, heartbeat_at: undefined }, 131000)).toEqual({ label: "最近心跳：尚未记录", stale: false });
    expect(heartbeatStatus(task, 130000)).toEqual({ label: "最近心跳：30 秒前", stale: false });
    expect(heartbeatStatus(task, 131000)).toEqual({ label: "最近心跳：31 秒前", stale: true });
    expect(heartbeatStatus(task, 99000).label).toBe("最近心跳：0 秒前");
  });
  it.each(["fixed", "cancelled", "interrupted", "blocked", "waiting_input"])("does not turn %s into a stale running task", status => {
    expect(heartbeatStatus({ ...task, status }, 500000).stale).toBe(false);
  });
  it("names each actual output source and preparation revision", () => {
    const source = { id: "example", number: 3, current: false };
    expect(progressSourceLabel({ ...source, kind: "prepare" })).toBe("准备第 3 版");
    expect(progressSourceLabel({ ...source, kind: "prepare", number: null })).toBe("复现准备");
    expect(progressSourceLabel({ ...source, kind: "coding" })).toBe("第 3 次修复");
    expect(progressSourceLabel({ ...source, kind: "review" })).toBe("第 3 次审核");
    expect(repairRoundLabel(task)).toBe("第 2 次修复");
    expect(repairRoundLabel({ ...task, stage: "auto_correcting", preparation_revisions: [{ revision: 4, status: "running" }] })).toBe("准备第 4 版");
    expect(repairRoundLabel({ ...task, stage: "prepare_reproducer", preparation_revisions: [] })).toBeNull();
  });
});


it("displays success groups independently from elapsed time and legacy budgets", async () => {
  const { budgetLabel } = await import("./progress");
  const base = { task_id: "test", status: "running", stage: "audit", elapsed_seconds: 2000,
    options: { model: "test", reasoning_effort: "medium", max_attempts: 3 } };
  expect(budgetLabel({ ...base, options: { ...base.options, budget: { mode: "fixed_groups", count: 3 } },
    governance_summary: { accepted_groups: 1 } })).toBe("已验收 1/3 组 · 已用 2000 秒");
  expect(budgetLabel({ ...base, options: { ...base.options, budget: { mode: "time", seconds: 7200 } } })).toContain("总时限 7200 秒");
  expect(budgetLabel({ ...base, options: { ...base.options, timeout_seconds: 1080 } })).toContain("总时限 1080 秒");
  expect(budgetLabel({ ...base, options: { ...base.options, budget: { mode: "discovered_groups", count: 1 } },
    governance_summary: { discovered_groups: 3, selected_groups: 1, accepted_groups: 0 } })).toBe(
    "已发现 3 组（目标 1 组） · 本轮选中 1 组 · 已验收 0 组 · 已用 2000 秒");
});
