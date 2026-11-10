import { afterEach, expect, it, vi } from "vitest";
import { developmentApi, sourceStateLabel, developmentTaskLabel } from "./developmentApi";
afterEach(() => vi.unstubAllGlobals());
it("keeps source, task and install calls scoped and never includes paths or commands", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await developmentApi.status("studio:a/b", signal);
  await developmentApi.start("studio:a/b", "request", "brief replies");
  await developmentApi.action("studio:a/b", "repair-abc", "install");
  expect(fetch.mock.calls[0]).toEqual(["/api/applications/studio%3Aa%2Fb/development", expect.objectContaining({ cache: "no-store", signal })]);
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ id: "request", goal: "brief replies" });
  expect(fetch.mock.calls[2][0]).toBe("/api/applications/studio%3Aa%2Fb/development/tasks/repair-abc/install");
  expect(fetch.mock.calls[2][1].body).toBe("{}");
});
it("does not show raw structured validation data as an error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: [{ input: "PRIVATE-VALUE" }] }) }));
  await expect(developmentApi.prepare("studio:a")).rejects.toThrow("应用开发操作失败");
});
it("distinguishes accepted, installed, pending cancellation and real execution", () => {
  expect(sourceStateLabel("installed")).toBe("验收版本已安装");
  expect(developmentTaskLabel("fixed")).toBe("独立验收通过");
  expect(developmentTaskLabel("cancel_requested")).toBe("正在请求取消");
  expect(developmentTaskLabel("interrupted")).toBe("已中断");
  expect(developmentTaskLabel("PRIVATE-VALUE")).toBe("状态待核对");
});
