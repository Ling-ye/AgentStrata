import { afterEach, describe, expect, it, vi } from "vitest";
import { harnessApi, sourceLabel, stageLabel, repairStatusLabel, type RepairTask } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("independent Harness source selection", () => {
  it("loads robot evidence through the Harness BFF", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ kind: "robot_task" }) });
    vi.stubGlobal("fetch", fetch);
    await harnessApi.load("robot_task", "run-source", "sample");
    expect(fetch.mock.calls[0][0]).toBe("/api/harness/sources/load");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ kind: "robot_task", source_id: "run-source", bot_id: "sample" });
  });
  it("queries paginated repair history independently of source services", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ tasks: [], total: 0 }) });
    vi.stubGlobal("fetch", fetch);
    await harnessApi.history(3, "eval a", "blocked");
    expect(fetch.mock.calls[0][0]).toBe("/api/harness/tasks?page=3&search=eval+a&status=blocked");
  });
  it("reports explicit backend blockers", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: { message: "证据已过期" } }) }));
    await expect(harnessApi.load("robot_task", "run-source", "sample")).rejects.toThrow("证据已过期");
  });
  it("submits multiline feedback with robot tasks and returns its saved snapshot", async () => {
    const feedback = { repair_hint: "检查分词\n及输入处理", expected_behavior: "保留空格\n和换行" };
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ task_id: "repair-example", source: { feedback } }) });
    vi.stubGlobal("fetch", fetch);
    const body = { source_kind: "robot_task" as const, bot_id: "sample", run_id: "run-example", feedback,
      request_id: "request-example", model: "test-model", reasoning_effort: "medium", max_attempts: 3, timeout_seconds: 7200 };
    const result = await harnessApi.start(body);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body);
    expect(result.source.feedback).toEqual(feedback);
  });
  it("distinguishes task sources and test preparation stages", () => {
    expect(sourceLabel({ source: { kind: "robot_task", run_id: "run-source" } } as RepairTask)).toBe("机器人任务 run-source");
    expect(stageLabel("prepare_reproducer")).toBe("建立复现测试");
    expect(stageLabel("verify-2")).toBe("第 2 轮复测");
  });
});


it("keeps local commit and main inclusion as separate facts", () => {
  const task = { status: "fixed", local_commit: { sha: "verified-sha" }, commit_in_main: false } as RepairTask;
  expect(repairStatusLabel(task)).toBe("已本地提交");
  expect(repairStatusLabel({ ...task, commit_in_main: true })).toBe("已进入本地 main");
  expect(stageLabel("review")).toBe("AI 审核");
  expect(stageLabel("commit")).toBe("本地提交");
});
