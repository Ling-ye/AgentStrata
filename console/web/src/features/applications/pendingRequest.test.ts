import { afterEach, expect, it, vi } from "vitest";
import { applicationRequestKey, clearAcknowledgedRequest, readPendingRequest, savePendingRequest } from "./pendingRequest";
import { developmentApi } from "./developmentApi";
afterEach(() => vi.unstubAllGlobals());
function storage() {
  const entries = new Map<string, string>();
  return { getItem: (key: string) => entries.get(key) ?? null, setItem: (key: string, value: string) => { entries.set(key, value); }, removeItem: (key: string) => { entries.delete(key); } };
}
const original = { id: "a".repeat(32), text: "回复时先给出简短结论" };

it("restores an uncertain request after refresh and isolates configuration, development and applications", () => {
  const saved = storage(), key = applicationRequestKey("qq-configuration", "studio:a");
  savePendingRequest(saved, key, original);
  expect(readPendingRequest(saved, key)).toEqual(original);
  expect(readPendingRequest(saved, applicationRequestKey("qq-development", "studio:a"))).toBeUndefined();
  expect(readPendingRequest(saved, applicationRequestKey("qq-configuration", "studio:b"))).toBeUndefined();
});

it("keeps a newer request when an old response arrives after navigation", () => {
  const saved = storage(), key = applicationRequestKey("qq-development", "studio:a");
  const newer = { id: "b".repeat(32), text: "新目标" };
  savePendingRequest(saved, key, newer);
  expect(clearAcknowledgedRequest(saved, key, original)).toBe(false);
  expect(readPendingRequest(saved, key)).toEqual(newer);
  expect(clearAcknowledgedRequest(saved, key, { ...newer, text: "被更换的目标" })).toBe(false);
  expect(clearAcknowledgedRequest(saved, key, newer)).toBe(true);
  expect(readPendingRequest(saved, key)).toBeUndefined();
});

it.each(["broken-json", JSON.stringify({ id: "bad", text: "goal" }), JSON.stringify({ id: original.id, text: "" })])("does not invent a new request for damaged storage", raw => {
  expect(() => readPendingRequest({ getItem: () => raw }, "scope")).toThrow();
});

it("retains the original request if clearing browser storage fails", () => {
  const saved = storage(); savePendingRequest(saved, "scope", original);
  const broken = { getItem: saved.getItem, removeItem: () => { throw new Error("storage unavailable"); } };
  expect(() => clearAcknowledgedRequest(broken, "scope", original)).toThrow("storage unavailable");
  expect(readPendingRequest(saved, "scope")).toEqual(original);
});

it("refreshes only the original scoped request through GET without restarting development", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: original.id, state: "pending" }) });
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await developmentApi.request("studio:a/b", "request?1", signal);
  await developmentApi.task("studio:a/b", "repair-abc", signal);
  expect(fetch.mock.calls[0]).toEqual(["/api/applications/studio%3Aa%2Fb/development/requests/request%3F1", expect.objectContaining({ cache: "no-store", signal })]);
  expect(fetch.mock.calls[1][0]).toBe("/api/applications/studio%3Aa%2Fb/development/tasks/repair-abc");
  expect(fetch.mock.calls.every(([, options]) => !options.method && !options.body)).toBe(true);
});

it("retains an explicit validation status so only a known refusal can return the goal to editing", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: [{ input: "PRIVATE" }] }), { status: 422 })));
  await expect(developmentApi.start("studio:a", original.id, original.text)).rejects.toMatchObject({ status: 422, message: "应用开发操作失败，请核对原请求状态" });
});
