import { readinessSummary } from "./ImprovementReadiness";
import { afterEach, describe, expect, it, vi } from "vitest";
import { applicationHref, applicationLocation, leavesApplication, localLink, managementStateLabel, typeLabel } from "./navigation";
import { applicationsApi, applicationBuildUrl } from "./api";
afterEach(() => vi.unstubAllGlobals());
describe("application identity and navigation", () => {
  it("roundtrips opaque source-qualified identities without confusing display names", () => {
    for (const id of ["repo:a/b", "studio:abc", "plugin:x:a?b"]) {
      expect(applicationLocation(applicationHref(id, "configuration"))).toEqual({ id, tab: "configuration" });
    }
  });
  it("falls back invalid tabs, and guards only departure from the current application", () => {
    expect(applicationLocation("#applications?application=studio%3Aa&tab=unknown").tab).toBe("overview");
    expect(leavesApplication("#applications?application=studio%3Aa&tab=improve", "studio:a")).toBe(false);
    expect(leavesApplication("#applications?application=studio%3Ab", "studio:a")).toBe(true);
    expect(leavesApplication("#models", "studio:a")).toBe(true);
  });
  it("never follows external or executable control links from a descriptor", () => {
    expect(localLink("javascript:alert(1)")).toBeUndefined();
    expect(localLink("https://example.test")).toBeUndefined();
    expect(localLink("#bots?instance=a")).toBe("#bots?instance=a");
    expect(typeLabel("companion")).toContain("桌面伙伴");
  });
});
describe("application API scoping", () => {
  it("encodes identity and passes abort signals to stale GETs", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }); vi.stubGlobal("fetch", fetch);
    const signal = new AbortController().signal;
    await applicationsApi.configuration("studio:a/b", signal);
    expect(fetch.mock.calls[0][0]).toBe("/api/applications/studio%3Aa%2Fb/configuration");
    expect(fetch.mock.calls[0][1]).toMatchObject({ cache: "no-store", signal });
  });
  it("keeps management conversation separate from business delivery and preserves request identity", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: "message" }) }); vi.stubGlobal("fetch", fetch);
    await applicationsApi.submit("studio:a", "message", "short replies");
    expect(fetch.mock.calls[0][0]).toBe("/api/applications/studio%3Aa/messages");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ id: "message", text: "short replies" });
  });
  it("binds proposal application to the selected application and message", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }); vi.stubGlobal("fetch", fetch);
    await applicationsApi.apply("studio:b", "m1");
    expect(fetch.mock.calls[0][0]).toBe("/api/applications/studio%3Ab/proposals/m1/apply");
  });
  it("does not dump structured validation values in errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: [{ input: "secret" }] }) }));
    await expect(applicationsApi.get("studio:a")).rejects.toThrow("应用操作失败");
  });
});

it("explains management states without implying business delivery", () => {
  expect(managementStateLabel("console-thinking")).toBe("正在整理需求");
  expect(managementStateLabel("configure")).toBe("配置提案待确认");
  expect(managementStateLabel("applied")).toBe("提案已应用");
  expect(managementStateLabel("future-state")).toBe("历史状态：future-state");
});
it("keeps preview operations separate and preserves original request identity", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ available: true, messages: [] }) }); vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await applicationsApi.chat("studio:a", signal);
  await applicationsApi.sendChat("studio:a", "request-1", "hello");
  await applicationsApi.sendChat("studio:a", "request-1", "hello");
  await applicationsApi.chatAction("studio:a", "request-1", "retry");
  await applicationsApi.chatAction("studio:a", "request-1", "abort");
  expect(fetch.mock.calls[0][0]).toBe("/api/applications/studio%3Aa/chat");
  expect(fetch.mock.calls[0][1].signal).toBe(signal);
  expect(fetch.mock.calls[1][1].body).toBe(fetch.mock.calls[2][1].body);
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ id: "request-1", text: "hello" });
  expect(fetch.mock.calls[3][0]).toBe("/api/applications/studio%3Aa/chat/request-1/retry");
  expect(fetch.mock.calls[4][0]).toBe("/api/applications/studio%3Aa/chat/request-1/abort");
  expect(fetch.mock.calls.every(([url]) => !url.includes("/messages"))).toBe(true);
});
it("uses server-scoped evidence endpoints instead of filtering global pages", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => [] }); vi.stubGlobal("fetch", fetch);
  await applicationsApi.evaluations("repo:a/b", 20);
  await applicationsApi.harness("repo:a/b", 2);
  expect(fetch.mock.calls[0][0]).toBe("/api/applications/repo%3Aa%2Fb/evaluations?offset=20&limit=20");
  expect(fetch.mock.calls[1][0]).toBe("/api/applications/repo%3Aa%2Fb/harness?page=2&limit=20");
});
it("constructs app-owned build downloads, not arbitrary artifact URLs", () => {
  expect(applicationBuildUrl("studio:a/b", "m?1")).toBe("/api/applications/studio%3Aa%2Fb/builds/m%3F1");
});
it("reads readiness for exactly one app without triggering an execution", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }); vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await applicationsApi.readiness("studio:a/b", signal);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toBe("/api/applications/studio%3Aa%2Fb/improvement-readiness");
  expect(fetch.mock.calls[0][1]).toMatchObject({ signal, cache: "no-store" });
  expect(fetch.mock.calls[0][1].method).toBeUndefined();
});
it("never treats source registration as verified evaluation execution", () => {
  const readiness = { application_id: "studio:a", scope: "source_registration" as const, source_supported: false, execution_verified: false, checks: [], requirements: [] };
  expect(readinessSummary(readiness)).toContain("暂不能直接运行");
  expect(readinessSummary({ ...readiness, source_supported: true })).toContain("尚未验证");
});