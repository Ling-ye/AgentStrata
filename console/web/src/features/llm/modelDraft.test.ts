import { afterEach, expect, it, vi } from "vitest";
import { llmApi, type ModelConfiguration } from "./api";
import { leavesModelsPage, modelDraftAfterSave } from "./modelDraft";

afterEach(() => vi.unstubAllGlobals());

function configuration(revision: string): ModelConfiguration {
  return { connections: { local: { kind: "openai_compatible", timeout: 120, auth: { mode: "api_key", key_env: "MODEL_KEY" } } },
    profiles: { chat: { connection: "local", model: "original" } }, bindings: { chat: "chat" },
    revision, source: "defaults", resolved: {} };
}

it("uses the acknowledged configuration when no edits followed submission", () => {
  const saved = { ...configuration("saved-revision"), source: "saved" as const };
  expect(modelDraftAfterSave(configuration("old-revision"), saved, false)).toEqual(saved);
});

it("retains connection, profile and binding edits while saving without mutating either input", () => {
  const current = configuration("old-revision");
  current.connections.local.timeout = 122;
  current.profiles.newer = { connection: "local", model: "new-model" };
  current.bindings.chat = "newer";
  const saved = { ...configuration("saved-revision"), source: "saved" as const };
  const before = structuredClone({ current, saved });
  expect(modelDraftAfterSave(current, saved, true)).toEqual({ ...current, revision: "saved-revision", source: "saved" });
  expect({ current, saved }).toEqual(before);
});

it("submits retained edits with the acknowledged revision on the next explicit save", async () => {
  const current = configuration("old-revision");
  current.connections.local.timeout = 122;
  const saved = { ...configuration("saved-revision"), source: "saved" as const };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => saved });
  vi.stubGlobal("fetch", fetch);
  await llmApi.save(modelDraftAfterSave(current, saved, true));
  expect(fetch).toHaveBeenCalledWith("/api/llm/config", expect.objectContaining({ method: "PUT" }));
  const body = JSON.parse(fetch.mock.calls[0][1].body);
  expect(body.connections.local.timeout).toBe(122);
  expect(body.revision).toBe("saved-revision");
  expect(body).not.toHaveProperty("source");
});

it.each(["#models", "#models?connection=local"])("keeps navigation %s within the same model draft", hash => {
  expect(leavesModelsPage(hash)).toBe(false);
});

it.each(["#applications", "#bots?instance=qq", ""])("asks before leaving the model draft for %s", hash => {
  expect(leavesModelsPage(hash)).toBe(true);
});
