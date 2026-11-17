import { afterEach, expect, it, vi } from "vitest";
import { canStartNewImport, importStateLabel, live2dImportApi, live2dImportKey, readLive2DImport } from "./live2dImport";
afterEach(() => vi.unstubAllGlobals());
it("restores the exact import identity without side effects", () => {
  expect(readLive2DImport({ getItem: key => key === live2dImportKey ? "a".repeat(32) : null })).toBe("a".repeat(32));
  expect(readLive2DImport({ getItem: () => null })).toBeUndefined();
  expect(() => readLive2DImport({ getItem: () => "../../path" })).toThrow("导入请求记录无效");
});
it("polls a stable request separately from explicit initial selection and retry", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ state: "selecting" }) }); vi.stubGlobal("fetch", fetch);
  const id = "a".repeat(32), signal = new AbortController().signal;
  await live2dImportApi.start(id);
  await live2dImportApi.status(id, signal);
  await live2dImportApi.start(id);
  await live2dImportApi.retry(id);
  expect(fetch.mock.calls[0][0]).toBe("/api/applications/imports/live2d");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ id });
  expect(fetch.mock.calls[1][0]).toBe("/api/applications/imports/" + id);
  expect(fetch.mock.calls[1][1]).toMatchObject({ signal, cache: "no-store" });
  expect(fetch.mock.calls[1][1].method).toBeUndefined();
  expect(fetch.mock.calls[0][1].body).toEqual(fetch.mock.calls[2][1].body);
  expect(fetch.mock.calls[3][0]).toBe("/api/applications/imports/" + id + "/retry");
  expect(fetch.mock.calls[3][1].body).toBe("{}");
});
it("allows replacement only after known failed or cancelled outcomes", () => {
  for (const state of ["pending", "selecting", "importing", "imported", "recovery_required"] as const) {
    expect(canStartNewImport({ id: "a", state, application_id: null, message: "", retryable: false })).toBe(false);
  }
  for (const state of ["cancelled", "failed"] as const) expect(canStartNewImport({ id: "a", state, application_id: null, message: "", retryable: true })).toBe(true);
  expect(canStartNewImport()).toBe(false);
  expect(importStateLabel("imported")).toBe("公开基线已导入");
  expect(importStateLabel("unknown")).toBe("状态待核对");
});

it("confirms closing the old picker only for an explicit recovery retry", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }); vi.stubGlobal("fetch", fetch);
  await live2dImportApi.retry("a".repeat(32), true);
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ selection_closed: true });
});
