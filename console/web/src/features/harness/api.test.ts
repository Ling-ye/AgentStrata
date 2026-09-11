import { afterEach, describe, expect, it, vi } from "vitest";
import { caseKey, harnessApi, selectedCase, sourceLabel, stageLabel, type RepairTask, type SourcePreview } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("independent Harness source selection", () => {
  it("keeps the selected target when Case IDs repeat", () => {
    const failures = ["first", "second"].map(target_id => ({ case_ref: "suite:case", case_id: "case", target_id }));
    const preview = { kind: "evaluation", bot_id: "sample", blockers: [], history: [], failures } satisfies SourcePreview;
    expect(selectedCase(preview, caseKey(failures[1]))?.target_id).toBe("second");
    expect(selectedCase(preview, "stale-selection")).toBeUndefined();
  });
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
  it("distinguishes task sources and test preparation stages", () => {
    expect(sourceLabel({ source: { kind: "robot_task", run_id: "run-source" } } as RepairTask)).toBe("机器人任务 run-source");
    expect(stageLabel("prepare_reproducer")).toBe("建立复现测试");
    expect(stageLabel("verify-2")).toBe("第 2 轮复测");
  });
});
