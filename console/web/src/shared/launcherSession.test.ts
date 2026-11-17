import { expect, it, vi } from "vitest";
import { prepareLauncherSession } from "./launcherSession";
it("leaves ordinary routes alone and performs no authentication request", () => {
  const replaceState = vi.fn(), send = vi.fn();
  expect(prepareLauncherSession({ hash: "#models", pathname: "/", search: "" }, { replaceState }, send)).toBeUndefined();
  expect(replaceState).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled();
});
it("strips launcher secrets before network work and shares strict-mode retries", async () => {
  const replaceState = vi.fn();
  const send = vi.fn().mockImplementation(async () => {
    expect(replaceState).toHaveBeenCalledWith(null, "", "/?view=studio#applications");
    return { ok: true };
  });
  const login = prepareLauncherSession({ hash: "#login=TEST-TOKEN", pathname: "/", search: "?view=studio" }, { replaceState }, send)!;
  await Promise.all([login(), login()]);
  expect(send).toHaveBeenCalledTimes(1);
  expect(send.mock.calls[0]).toEqual(["/api/login", expect.objectContaining({ method: "POST", credentials: "same-origin", cache: "no-store", body: '{"token":"TEST-TOKEN"}' })]);
});
it("fails closed, redacts server errors and retries only the original exchange", async () => {
  const send = vi.fn().mockResolvedValueOnce({ ok: false, json: async () => ({ detail: "SECRET" }) }).mockResolvedValueOnce({ ok: true });
  const login = prepareLauncherSession({ hash: "#login=TEST-TOKEN", pathname: "/", search: "" }, { replaceState: vi.fn() }, send)!;
  await expect(login()).rejects.toThrow("工作台登录未完成");
  await expect(login()).resolves.toBeUndefined();
  expect(send.mock.calls[0][1].body).toEqual(send.mock.calls[1][1].body);
});
