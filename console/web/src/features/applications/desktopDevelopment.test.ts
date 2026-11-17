import { afterEach, describe, expect, it, vi } from "vitest";
import { acknowledgedDevelopment, canonicalDevelopmentText, DESKTOP_REQUIREMENT_LIMIT, desktopBusy, developmentTextLength, readPendingDevelopment, readyDesktopBuild, rejectedDevelopmentRequest, savePendingDevelopment } from "./desktopDevelopment";
import { ApplicationRequestError, applicationsApi } from "./api";
import { managementStateLabel } from "./navigation";
afterEach(() => vi.unstubAllGlobals());
function memoryStorage() {
  const values = new Map<string, string>();
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
}
const request = { id: "a".repeat(32), text: "做一个桌面伙伴" };
const message = { ...request, state: "thinking", reply: "" };
describe("desktop request recovery", () => {
  it("persists identity and text before sending and restores only the same application", () => {
    const storage = memoryStorage();
    savePendingDevelopment(storage, "studio:a", request);
    expect(readPendingDevelopment(storage, "studio:a")).toEqual(request);
    expect(readPendingDevelopment(storage, "studio:b")).toBeUndefined();
    savePendingDevelopment(storage, "studio:a");
    expect(readPendingDevelopment(storage, "studio:a")).toBeUndefined();
  });
  it("does not acknowledge a different request or changed text", () => {
    expect(acknowledgedDevelopment(request, [message])).toBe(true);
    expect(acknowledgedDevelopment(request, [{ ...message, id: "b".repeat(32) }])).toBe(false);
    expect(acknowledgedDevelopment(request, [{ ...message, text: "different" }])).toBe(false);
  });
  it("fails closed for corrupt recovery records rather than assigning a fresh identity", () => {
    expect(() => readPendingDevelopment({ getItem: () => '{"id":"bad","text":"x"}' }, "studio:a")).toThrow("待确认请求记录无效");
    expect(() => readPendingDevelopment({ getItem: () => "invalid" }, "studio:a")).toThrow();
  });
  it("keeps each nonterminal stage busy and distinguishes builds from actual installation", () => {
    for (const state of ["thinking", "starting", "developing", "verifying", "packaging"]) expect(desktopBusy([{ ...message, state }])).toBe(true);
    for (const state of ["discuss", "failed", "ready"]) expect(desktopBusy([{ ...message, state }])).toBe(false);
    expect(readyDesktopBuild({ ...message, state: "ready", build: request.id })).toBe(true);
    expect(readyDesktopBuild({ ...message, state: "ready", build: "other" })).toBe(false);
    expect(readyDesktopBuild({ ...message, state: "packaging", build: request.id })).toBe(false);
    expect(managementStateLabel("ready")).toBe("已验收，构建可下载");
  });
  it("retries the original app-owned task without receiving paths or shell commands", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) }); vi.stubGlobal("fetch", fetch);
    await applicationsApi.retryDevelopment("studio:a/b", "m?1");
    expect(fetch.mock.calls[0][0]).toBe("/api/applications/studio%3Aa%2Fb/messages/m%3F1/retry");
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: "POST", body: "{}" });
  });
});

it("shows legacy active rows as read-only without locking new development", () => {
  expect(desktopBusy([{ ...message, state: "developing", read_only: true, recovery_message: "旧协议任务只读保留" }])).toBe(false);
  expect(desktopBusy([{ ...message, state: "developing", read_only: false }])).toBe(true);
});

it("keeps an unresolved old operation blocked for its own application", () => {
  expect(desktopBusy([{ ...message, state: "developing", read_only: true, blocks_development: true }])).toBe(true);
  expect(desktopBusy([{ ...message, state: "failed", read_only: true }])).toBe(false);
  expect(desktopBusy([])).toBe(false);
});

it("acknowledges a stored request after the backend strips Unicode boundary whitespace", () => {
  const pending = { ...request, text: "\u0085做一个桌面伙伴\u001c" };
  expect(acknowledgedDevelopment(pending, [message])).toBe(true);
  expect(acknowledgedDevelopment(pending, [{ ...message, text: "different" }])).toBe(false);
});

it("retains an explicit API validation status for editing a rejected requirement", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "invalid requirement" }), { status: 422 })));
  await expect(applicationsApi.submit("studio:a", request.id, request.text)).rejects.toMatchObject({ message: "invalid requirement", status: 422 });
});

it("counts request limits by Unicode characters rather than UTF-16 units", () => {
  expect(developmentTextLength("需".repeat(16000))).toBe(DESKTOP_REQUIREMENT_LIMIT);
  expect(developmentTextLength("😀".repeat(16000))).toBe(DESKTOP_REQUIREMENT_LIMIT);
  expect(developmentTextLength("😀".repeat(16001))).toBeGreaterThan(DESKTOP_REQUIREMENT_LIMIT);
});

it("matches backend boundary whitespace while preserving interior and BOM text", () => {
  for (const whitespace of ["\t\n\r", "\u001c\u001d\u001e\u001f", "\u0085", "\u00a0", "\u1680", "\u2000\u200a", "\u2028\u2029", "\u202f\u205f\u3000"]) {
    expect(canonicalDevelopmentText(whitespace + request.text + whitespace)).toBe(request.text);
  }
  expect(canonicalDevelopmentText("\u0085first\u0085second\u0085")).toBe("first\u0085second");
  expect(canonicalDevelopmentText("\u0085\ufeffgoal\ufeff\u0085")).toBe("\ufeffgoal\ufeff");
});

it("releases only explicit validation rejection or a known empty-input rejection", () => {
  expect(rejectedDevelopmentRequest(new ApplicationRequestError("invalid", 422), request)).toBe(true);
  expect(rejectedDevelopmentRequest(new ApplicationRequestError("empty", 400), { ...request, text: "\u0085\u001c" })).toBe(true);
  for (const status of [400, 401, 403, 404, 409, 500, 503]) {
    expect(rejectedDevelopmentRequest(new ApplicationRequestError("unknown outcome", status), request)).toBe(false);
  }
  expect(rejectedDevelopmentRequest(new Error("network interrupted"), request)).toBe(false);
  expect(rejectedDevelopmentRequest(new SyntaxError("invalid response"), request)).toBe(false);
});

it("restores a legacy Unicode-only pending request so its explicit refusal can return it to edit", () => {
  const storage = memoryStorage(), pending = { ...request, text: "\u0085" };
  savePendingDevelopment(storage, "studio:a", pending);
  expect(readPendingDevelopment(storage, "studio:a")).toEqual(pending);
  expect(canonicalDevelopmentText(pending.text)).toBe("");
});
