import { afterEach, expect, it, vi } from "vitest";
import { credentialStateLabel, developmentSetupApi } from "./developmentSetup";
afterEach(() => vi.unstubAllGlobals());
it("reads saved readiness without performing authorization or model execution", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }); vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await developmentSetupApi.status(signal);
  expect(fetch).toHaveBeenCalledWith("/api/development/setup", expect.objectContaining({ signal, cache: "no-store" }));
  expect(fetch.mock.calls[0][1].method).toBeUndefined();
  await developmentSetupApi.login("worker");
  expect(fetch.mock.calls[1][0]).toBe("/api/development/setup/login/worker");
  expect(fetch.mock.calls[1][1]).toMatchObject({ method: "POST", body: "{}" });
});
it("does not treat unknown credential state as successful authorization", () => {
  expect(credentialStateLabel("ready")).toBe("凭据已准备");
  expect(credentialStateLabel("unconfigured")).toBe("尚未配置此订阅通道");
  expect(credentialStateLabel("new-state")).toBe("状态待核对");
});
